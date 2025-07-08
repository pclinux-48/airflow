from datetime import datetime
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
import pandas as pd
import requests
import logging
from io import StringIO

# A DAG é instanciada aqui e a variável 'dag' a contém
dag = DAG(
    'fin_cotacoes_bcb_classic',
    schedule_interval='@daily',
    default_args={
        'owner': 'airflow',
        'retries': 1,
        'start_date': datetime(2024, 1, 1),
        'catchup': False
    },
    tags=["bcb"],
    doc_md="DAG para extrair, transformar e carregar cotações de moedas do Banco Central do Brasil."
)

##### EXTRACT #######

def extract(**kwargs):
    """
    Extrai os dados de cotações de moedas do site do Banco Central do Brasil.
    A URL é montada usando a data de execução da DAG.
    """
    ds_nodash = kwargs["ds_nodash"]
    base_url = "https://www4.bcb.gov.br/Download/fechamento/"
    full_url = base_url + ds_nodash + ".csv"
    logging.info(f"Iniciando extração da URL: {full_url}")

    try:
        response = requests.get(full_url, timeout=10)
        response.raise_for_status()
        csv_data = response.content.decode('utf-8')
        logging.info("Extração concluída com sucesso.")
        # Não precisa mais retornar o dado via XCom se a próxima tarefa não for usar
        # Mas vamos manter para fins de depuração, a próxima tarefa irá puxar o dado.
        return csv_data
    except requests.exceptions.RequestException as e:
        logging.warning(f"Não foi possível extrair dados da URL (pode ser um dia sem cotação): {e}")
        return None

extract_task = PythonOperator(
    task_id='extract',
    python_callable=extract,
    dag=dag
)


###### CREATE TABLE ##############

create_table_ddl = """
CREATE TABLE IF NOT EXISTS cotacoes (
    dt_fechamento DATE,
    cod_moeda TEXT,
    tipo_moeda TEXT,
    desc_moeda TEXT,
    taxa_compra REAL,
    taxa_venda REAL,
    paridade_compra REAL,
    paridade_venda REAL,
    data_processamento TIMESTAMP,
    CONSTRAINT table_pk PRIMARY KEY (dt_fechamento, cod_moeda)
);
"""

create_table_postgres = SQLExecuteQueryOperator(
    task_id="create_table_postgres",
    conn_id="airflow-postgres",
    sql=create_table_ddl,
    dag=dag
)


####### -------- TRANSFORM AND LOAD ------  ############

def transform_and_load(**kwargs):
    """
    Recebe os dados da extração, transforma com Pandas e carrega no PostgreSQL.
    Une as lógicas de Transform e Load para evitar o uso excessivo de XCom.
    """
    cotacoes_csv = kwargs['ti'].xcom_pull(task_ids='extract')
    
    if cotacoes_csv is None:
        logging.warning("Nenhum dado recebido da tarefa de extração. Pulando transformação e carga.")
        return

    # --- Lógica de Transformação ---
    csv_string_io = StringIO(cotacoes_csv)
    column_names = [
        "dt_fechamento", "cod_moeda", "tipo_moeda", "desc_moeda",
        "taxa_compra", "taxa_venda", "paridade_compra", "paridade_venda"
    ]
    data_types = {
        "cod_moeda": str, "tipo_moeda": str, "desc_moeda": str,
        "taxa_compra": float, "taxa_venda": float, "paridade_compra": float, "paridade_venda": float
    }
    df = pd.read_csv(
        csv_string_io, sep=";", decimal=",", thousands=".", header=None,
        names=column_names, dtype=data_types, parse_dates=["dt_fechamento"], encoding="utf-8"
    )
    df['data_processamento'] = datetime.now()
    logging.info(f"DataFrame transformado com {len(df)} linhas.")

    # --- Lógica de Carga (Load) ---
    table_name = "cotacoes"
    postgres_hook = PostgresHook(postgres_conn_id="airflow-postgres")
    
    target_fields = list(df.columns)
    
    postgres_hook.insert_rows(
        table=table_name,
        rows=df.values.tolist(),
        target_fields=target_fields,
        replace=True,
        replace_index=['dt_fechamento', 'cod_moeda']
    )
    logging.info(f"Carregados/Atualizados {len(df)} registros na tabela {table_name}.")


transform_and_load_task = PythonOperator(
    task_id='transform_and_load',
    python_callable=transform_and_load,
    dag=dag
)


# Definindo o fluxo de execução das tarefas
# A tarefa de carga e transformação depende da extração e da criação da tabela.
[extract_task, create_table_postgres] >> transform_and_load_task