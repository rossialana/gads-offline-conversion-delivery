#!/usr/bin/env python3
"""
Sincroniza vendas efetivadas (Google Ads) do Bigin para a planilha
"Conversões Offline Google Ads" no formato que ELA JÁ TEM configurado
(11 colunas, usadas no wizard "Central de dados" do Google Ads):

Conversion Time | ID Transaction | Google Click ID | Conversion Name |
Conversion Phone | Conversion Email | Conversion Value | Conversion Currency |
GBRAID | WBRAID | Endereço IP

Mantém um arquivo de estado (sync_state.json), versionado no repo pelo
próprio workflow do GitHub Actions, para nunca reenviar o mesmo negócio
duas vezes -- evita duplicar conversão no Google Ads.

Como segurança extra (caso o estado fique desatualizado por qualquer
motivo), antes de escrever também é checado se o "ID Transaction" já
existe entre as linhas atuais da planilha -- nesse caso a linha é
pulada mesmo que o estado local não soubesse dela.

Requer:
  pip install gspread google-auth requests

Variáveis de ambiente esperadas (Secrets no GitHub Actions):
  BIGIN_CLIENT_ID
  BIGIN_CLIENT_SECRET
  BIGIN_REFRESH_TOKEN
  GOOGLE_SERVICE_ACCOUNT_JSON
  GOOGLE_SHEET_ID    (padrão: 1y5fc4IxlpFfeJ23Q-NuaW-aoyjoGQBcdw33FI8aK0BM)
  GOOGLE_SHEET_TAB   (padrão: "Página1" -- é a aba onde o cabeçalho real está)
"""

import json
import os
import sys
from datetime import datetime

import requests

STATE_FILE = os.environ.get("STATE_FILE", "sync_state.json")
GOOGLE_SHEET_ID = os.environ.get(
    "GOOGLE_SHEET_ID", "1y5fc4IxlpFfeJ23Q-NuaW-aoyjoGQBcdw33FI8aK0BM"
)
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Página1")
CONVERSION_NAME = os.environ.get("CONVERSION_NAME", "Venda Efetivada (offline)")

HEADER = [
    "Conversion Time", "ID Transaction", "Google Click ID", "Conversion Name",
    "Conversion Phone", "Conversion Email", "Conversion Value",
    "Conversion Currency", "GBRAID", "WBRAID", "Endereço IP",
]

# Índice (0-based) da coluna "ID Transaction" dentro de HEADER/rows.
ID_TRANSACTION_COL = 1

# IMPORTANTE: o COQL do Bigin dá erro de sintaxe ao combinar mais de duas
# condições quando uma delas é "is not null" -- então filtramos só
# Stage + ID_ADS aqui, e Lead_Source em Python (o campo ID_ADS às vezes
# guarda o ID interno de anúncio do Meta em vez de um gclid real, quando o
# negócio veio do Meta Ads).
COQL_QUERY = (
    "select id, Deal_Name, Amount, Modified_Time, ID_ADS, Lead_Source, "
    "gbraid, wbraid, E_mail, Telefone, Telefone_do_Contato "
    "from Pipelines where ID_ADS is not null and Stage = 'Venda Efetivada' "
    "order by Modified_Time desc limit 200"
)

JUNK_VALUES = {"papap", "aaad", "teste", "test", "n/a", "-"}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"synced_ids": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_bigin_access_token():
    resp = requests.post(
        "https://accounts.zoho.com/oauth/v2/token",
        params={
            "refresh_token": os.environ["BIGIN_REFRESH_TOKEN"],
            "client_id": os.environ["BIGIN_CLIENT_ID"],
            "client_secret": os.environ["BIGIN_CLIENT_SECRET"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_deals(access_token):
    resp = requests.post(
        "https://www.zohoapis.com/bigin/v2/coql",
        headers={
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json",
        },
        json={"select_query": COQL_QUERY},
        timeout=30,
    )
    if resp.status_code == 204:
        return []
    resp.raise_for_status()
    return resp.json().get("data", [])


def is_valid_gclid(value):
    if not value:
        return False
    value = str(value).strip()
    return value.lower() not in JUNK_VALUES


def is_google_ads_deal(deal):
    # ID_ADS às vezes guarda o ID interno de anúncio do Meta (numérico puro,
    # sem o prefixo Cj0K/CjwK/EAIa dos gclids reais do Google). Como já
    # filtramos por Lead_Source aqui, isso evita pegar negócios do Meta Ads
    # que tenham (por engano) um valor em ID_ADS.
    return (deal.get("Lead_Source") or "").strip() == "Google Ads"


def parse_amount(value):
    """Converte o campo Amount do Bigin para float puro.

    O Bigin/Zoho às vezes devolve o valor como string já formatada no
    locale BR (vírgula decimal, ex.: "1.500,00" ou "150,00"). Se isso for
    escrito na planilha como texto, o Google Ads falha na importação com
    "Column 'Conversion_Value...' cannot be converted to a 'double'"
    (visto nos erros de 29/09/2026 -- 13 linhas rejeitadas, corrigidas
    manualmente trocando vírgula por ponto).

    Convertendo sempre para float aqui, o valor é escrito na planilha como
    número puro (não texto), então não depende do locale da planilha nem
    de como o Bigin formatou o valor originalmente.
    """
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("R$", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        # "1.500,00" -> milhar com ponto, decimal com vírgula
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        # "150,00" -> vírgula é o decimal
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def build_row(deal):
    modified = deal["Modified_Time"]  # ex: 2026-09-25T16:19:20-03:00
    dt = datetime.fromisoformat(modified)
    conversion_time = dt.strftime("%Y-%m-%d %H:%M:%S%z")
    conversion_time = conversion_time[:-2] + ":" + conversion_time[-2:]  # -0300 -> -03:00

    phone = deal.get("Telefone_do_Contato") or deal.get("Telefone") or ""
    email = deal.get("E_mail") or ""

    return [
        conversion_time,
        deal["id"],
        deal["ID_ADS"],
        CONVERSION_NAME,
        phone,
        email,
        parse_amount(deal.get("Amount")),
        "BRL",
        deal.get("gbraid") or "",
        deal.get("wbraid") or "",
        "",  # Endereço IP -- não capturado no Bigin hoje
    ]


def open_worksheet(gc):
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(GOOGLE_SHEET_TAB)
        existing_header = ws.row_values(1)
        if not existing_header:
            ws.append_row(HEADER)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=GOOGLE_SHEET_TAB, rows=1000, cols=len(HEADER))
        ws.append_row(HEADER)
    return ws


def get_existing_transaction_ids(ws):
    """IDs de negócio (coluna 'ID Transaction') já presentes na planilha.

    Usado como segunda camada de proteção contra duplicatas, além do
    sync_state.json -- cobre o caso do estado ficar desatualizado.
    """
    try:
        col_values = ws.col_values(ID_TRANSACTION_COL + 1)  # gspread é 1-based
    except Exception:
        return set()
    return set(col_values[1:])  # pula o cabeçalho


def write_to_sheet(rows):
    if not rows:
        return 0
    import gspread
    from google.oauth2.service_account import Credentials

    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    ws = open_worksheet(gc)

    existing_ids = get_existing_transaction_ids(ws)
    rows_to_write = [
        row for row in rows if str(row[ID_TRANSACTION_COL]) not in existing_ids
    ]
    skipped_dup_in_sheet = len(rows) - len(rows_to_write)
    if skipped_dup_in_sheet:
        print(
            f"Aviso: {skipped_dup_in_sheet} linha(s) já existiam na planilha "
            "(ID Transaction repetido) -- puladas para não duplicar."
        )

    if rows_to_write:
        ws.append_rows(rows_to_write, value_input_option="USER_ENTERED")
    return len(rows_to_write)


def main():
    dry_run = "--dry-run" in sys.argv
    state = load_state()
    synced = set(state["synced_ids"])

    access_token = get_bigin_access_token()
    deals = fetch_deals(access_token)

    new_rows = []
    newly_synced_ids = []
    skipped_no_gclid = 0
    skipped_not_google = 0

    for deal in deals:
        deal_id = deal["id"]
        if deal_id in synced:
            continue
        if not is_valid_gclid(deal.get("ID_ADS")):
            skipped_no_gclid += 1
            continue
        if not is_google_ads_deal(deal):
            skipped_not_google += 1
            continue
        new_rows.append(build_row(deal))
        newly_synced_ids.append(deal_id)

    print(f"Negócios encontrados (Stage=Venda Efetivada, ID_ADS preenchido): {len(deals)}")
    print(f"Já sincronizados antes: {len(deals) - len(new_rows) - skipped_no_gclid - skipped_not_google}")
    print(f"Sem gclid válido (pulados): {skipped_no_gclid}")
    print(f"Não são Google Ads / ID_ADS contaminado (pulados): {skipped_not_google}")
    print(f"Novos para enviar: {len(new_rows)}")
    for row in new_rows:
        print("  ", row)

    if dry_run:
        print("\n[--dry-run] Nada foi escrito na planilha nem no estado.")
        return

    written = write_to_sheet(new_rows)
    # Todo negócio processado nesta rodada (mesmo os pulados por já estarem
    # na planilha) entra no estado, para não ficar tentando de novo sempre.
    state["synced_ids"] = list(synced | set(newly_synced_ids))
    save_state(state)
    print(f"\n{written} linha(s) escrita(s) na planilha, aba '{GOOGLE_SHEET_TAB}'.")


if __name__ == "__main__":
    main()
