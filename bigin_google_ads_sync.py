#!/usr/bin/env python3
"""
Sincroniza vendas efetivadas (Google Ads) do Bigin para uma aba do Google Sheets
no formato que o Google Ads espera para "Conversions from Sheets"
(colunas: Google Click ID, Conversion Name, Conversion Time, Conversion Value,
Conversion Currency).

Mantém um arquivo de estado (sync_state.json) para nunca reenviar o mesmo
negócio duas vezes -- evita duplicar conversão no Google Ads.

Requer:
  pip install gspread google-auth requests

Variáveis de ambiente esperadas (configuradas como Secrets no GitHub Actions):
  BIGIN_CLIENT_ID       -- Zoho API self-client
  BIGIN_CLIENT_SECRET
  BIGIN_REFRESH_TOKEN
  GOOGLE_SERVICE_ACCOUNT_JSON  -- conteúdo do arquivo de credenciais (JSON como string)
  GOOGLE_SHEET_ID       -- ID da planilha (padrão: 1y5fc4IxlpFfeJ23Q-NuaW-aoyjoGQBcdw33FI8aK0BM)
  GOOGLE_SHEET_TAB      -- nome da aba (padrão: "Conversions")
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

STATE_FILE = os.environ.get("STATE_FILE", "sync_state.json")
GOOGLE_SHEET_ID = os.environ.get(
    "GOOGLE_SHEET_ID", "1y5fc4IxlpFfeJ23Q-NuaW-aoyjoGQBcdw33FI8aK0BM"
)
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "Conversions")
CONVERSION_NAME = os.environ.get("CONVERSION_NAME", "Venda Efetivada (offline)")

# Zoho COQL só permite algumas combinações de filtro por chamada; então
# filtramos Stage + Lead_Source aqui e validamos o formato do gclid em Python.
COQL_QUERY = (
    "select id, Deal_Name, Amount, Modified_Time, ID_ADS "
    "from Pipelines where Stage = 'Venda Efetivada' and Lead_Source = 'Google Ads' "
    "limit 200"
)

# A captura de gclid da LP está sendo corrigida; por ora aceitamos qualquer
# valor não vazio em ID_ADS (a query já filtra Lead_Source = 'Google Ads',
# então não há risco de pegar o ID de anúncio interno do Meta aqui). Só
# bloqueamos valores de teste óbvios.
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


def build_row(deal):
    # Conversion Time no formato que o Google Ads espera: "yyyy-MM-dd HH:mm:ss+HH:mm"
    modified = deal["Modified_Time"]  # ex: 2026-09-25T16:19:20-03:00
    dt = datetime.fromisoformat(modified)
    conversion_time = dt.strftime("%Y-%m-%d %H:%M:%S%z")
    conversion_time = conversion_time[:-2] + ":" + conversion_time[-2:]  # -0300 -> -03:00

    return [
        deal["ID_ADS"],
        CONVERSION_NAME,
        conversion_time,
        deal.get("Amount") or 0,
        "BRL",
    ]


def write_to_sheet(rows):
    if not rows:
        return
    import gspread
    from google.oauth2.service_account import Credentials

    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(GOOGLE_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=GOOGLE_SHEET_TAB, rows=1000, cols=10)
        ws.append_row(
            ["Google Click ID", "Conversion Name", "Conversion Time",
             "Conversion Value", "Conversion Currency"]
        )
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def main():
    dry_run = "--dry-run" in sys.argv
    state = load_state()
    synced = set(state["synced_ids"])

    access_token = get_bigin_access_token()
    deals = fetch_deals(access_token)

    new_rows = []
    newly_synced_ids = []
    skipped_no_gclid = 0

    for deal in deals:
        deal_id = deal["id"]
        if deal_id in synced:
            continue
        if not is_valid_gclid(deal.get("ID_ADS")):
            skipped_no_gclid += 1
            continue
        new_rows.append(build_row(deal))
        newly_synced_ids.append(deal_id)

    print(f"Negócios encontrados: {len(deals)}")
    print(f"Já sincronizados antes: {len(deals) - len(new_rows) - skipped_no_gclid}")
    print(f"Sem gclid válido (pulados): {skipped_no_gclid}")
    print(f"Novos para enviar: {len(new_rows)}")
    for row in new_rows:
        print("  ", row)

    if dry_run:
        print("\n[--dry-run] Nada foi escrito na planilha nem no estado.")
        return

    write_to_sheet(new_rows)
    state["synced_ids"] = list(synced | set(newly_synced_ids))
    save_state(state)
    print(f"\n{len(new_rows)} linha(s) escrita(s) na planilha '{GOOGLE_SHEET_TAB}'.")


if __name__ == "__main__":
    main()
