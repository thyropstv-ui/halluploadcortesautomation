import argparse
import json
import mimetypes
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/spreadsheets",
]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Sao_Paulo"))

ALIASES = {
    "video": {"video", "arquivo", "arquivo_video", "nome_do_arquivo", "file"},
    "titulo": {"titulo", "title", "nome_do_video", "nome"},
    "descricao": {"descricao", "description"},
    "tags": {"tags", "tag"},
    "thumbnail": {"thumbnail", "thumb", "miniatura", "capa"},
    "privacidade": {"privacidade", "privacy", "visibilidade"},
    "agendamento": {"agendamento", "data_hora", "publicar_em", "data_de_publicacao", "schedule"},
    "status": {"status", "situacao"},
    "link": {"link", "url"},
    "observacao": {"observacao", "obs", "erro", "log"},
}
PRIVACY = {
    "private": "private", "privado": "private",
    "unlisted": "unlisted", "nao_listado": "unlisted",
    "public": "public", "publico": "public",
}
DATE_FORMATS = [
    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
]

def norm(s):
    s = unicodedata.normalize("NFD", str(s).strip().lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def col_letter(i):
    s, i = "", i + 1
    while i:
        i, rem = divmod(i - 1, 26)
        s = chr(65 + rem) + s
    return s


def parse_schedule(text):
    for fmt in DATE_FORMATS:
        try:
            local = datetime.strptime(text.strip(), fmt).replace(tzinfo=TZ)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"data/hora inválida: '{text}' (use ex.: 15/11/2026 18:00)")
    utc = local.astimezone(timezone.utc)
    if utc <= datetime.now(timezone.utc):
        raise ValueError(f"agendamento '{text}' já passou; ajuste a data e volte o status para pendente")
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

def get_credentials():
    env_token = os.getenv("GOOGLE_TOKEN_JSON")
    creds = None
    if env_token:
        creds = Credentials.from_authorized_user_info(json.loads(env_token), SCOPES)
    elif os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if env_token:
            sys.exit("GOOGLE_TOKEN_JSON inválido ou expirado. Gere o token de novo com --auth.")
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
    if not env_token:
        Path(TOKEN_FILE).write_text(creds.to_json())
    return creds

def read_sheet(sheets, spreadsheet_id, tab):
    rows = (
        sheets.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=f"'{tab}'")
        .execute().get("values", [])
    )
    if not rows:
        sys.exit("A planilha está vazia (faltam os cabeçalhos na linha 1).")
    cols = {}
    for i, h in enumerate(rows[0]):
        n = norm(h)
        for canon, names in ALIASES.items():
            if n in names and canon not in cols:
                cols[canon] = i
    missing = [c for c in ("video", "titulo", "status") if c not in cols]
    if missing:
        sys.exit(f"Faltam colunas obrigatórias na planilha: {', '.join(missing)}")

    items = []
    for row_number, r in enumerate(rows[1:], start=2):
        item = {"row": row_number}
        for canon in ALIASES:
            i = cols.get(canon)
            item[canon] = r[i].strip() if i is not None and i < len(r) else ""
        items.append(item)
    return cols, items


def set_cells(sheets, spreadsheet_id, tab, cols, row, values):
    data = [
        {"range": f"'{tab}'!{col_letter(cols[k])}{row}", "values": [[v]]}
        for k, v in values.items() if k in cols
    ]
    if not data:
        return
    for attempt in range(4):
        try:
            sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"valueInputOption": "RAW", "data": data}
            ).execute()
            return
        except HttpError:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)

def list_folder(drive, folder_id):
    files, token = {}, None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name)",
            pageSize=200, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        for f in resp.get("files", []):
            files.setdefault(f["name"], f)
        token = resp.get("nextPageToken")
        if not token:
            return files


def download_file(drive, file_id, dest):
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(fh, request, chunksize=32 * 1024 * 1024)
        done = False
        while not done:
            status, done = dl.next_chunk()
            if status:
                print(f"    download {int(status.progress() * 100)}%", end="\r")
    print()


def remove_from_drive(drive, file_id, permanent):
    if permanent:
        drive.files().delete(fileId=file_id, supportsAllDrives=True).execute()
    else:
        drive.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()

def upload_video(youtube, path, item, publish_at):
    tags = [t.strip() for t in item["tags"].split(",") if t.strip()]
    privacy = PRIVACY.get(norm(item["privacidade"]), "private")
    status = {"selfDeclaredMadeForKids": False}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at)  # exigência da API
    else:
        status["privacyStatus"] = privacy
    body = {
        "snippet": {
            "title": item["titulo"][:100],
            "description": item["descricao"][:5000],
            "tags": tags,
            "categoryId": "22",
        },
        "status": status,
    }
    media = MediaFileUpload(str(path), chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response, retries = None, 0
    while response is None:
        try:
            progress, response = request.next_chunk()
            if progress:
                print(f"    upload {int(progress.progress() * 100)}%", end="\r")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 8:
                retries += 1
                time.sleep(2 ** retries)
                continue
            raise
    print()
    return response["id"]


def set_thumbnail(youtube, video_id, path):
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    youtube.thumbnails().set(
        videoId=video_id, media_body=MediaFileUpload(str(path), mimetype=mime)
    ).execute()


def is_stop_error(e):
    body = (e.content or b"").decode("utf-8", "ignore")
    return any(k in body for k in ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder-id", default=os.getenv("FOLDER_ID"))
    ap.add_argument("--spreadsheet-id", default=os.getenv("SPREADSHEET_ID"))
    ap.add_argument("--tab", default=os.getenv("SHEET_TAB", "Videos"))
    ap.add_argument("--max", type=int, default=5, help="máx. de vídeos por execução (cota diária ≈ 6)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep", action="store_true", help="não apaga nada do Drive")
    ap.add_argument("--permanent-delete", action="store_true", help="apaga de vez, sem lixeira")
    ap.add_argument("--auth", action="store_true", help="só faz o login e salva o token.json")
    args = ap.parse_args()

    creds = get_credentials()
    if args.auth:
        print(f"Login OK. Token salvo em {TOKEN_FILE}.")
        return
    if not args.folder_id or not args.spreadsheet_id:
        sys.exit("Informe --folder-id e --spreadsheet-id (ou as variáveis FOLDER_ID e SPREADSHEET_ID).")

    drive = build("drive", "v3", credentials=creds)
    youtube = build("youtube", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)

    cols, items = read_sheet(sheets, args.spreadsheet_id, args.tab)
    pending = [
        i for i in items
        if i["video"] and i["status"].lower() in ("", "pendente")
    ]
    if not pending:
        print("Nenhuma linha pendente.")
        return
    folder = list_folder(drive, args.folder_id)

    def mark(item, **values):
        set_cells(sheets, args.spreadsheet_id, args.tab, cols, item["row"], values)

    done, errors = 0, 0
    for item in pending:
        if done >= args.max:
            print(f"\nLimite de {args.max} vídeos por execução atingido; o resto fica para a próxima.")
            break
        print(f"\n▶ linha {item['row']}: {item['titulo']}  ({item['video']})")

        try:
            video_file = folder.get(item["video"])
            if not video_file:
                raise ValueError(f"vídeo '{item['video']}' não está na pasta do Drive")
            thumb_file = None
            if item["thumbnail"]:
                thumb_file = folder.get(item["thumbnail"])
                if not thumb_file:
                    raise ValueError(f"thumbnail '{item['thumbnail']}' não está na pasta do Drive")
            if not item["titulo"]:
                raise ValueError("título vazio")
            publish_at = parse_schedule(item["agendamento"]) if item["agendamento"] else None
        except ValueError as e:
            print(f"  ! {e}")
            errors += 1
            if not args.dry_run:
                mark(item, status="erro", observacao=str(e))
            continue

        when = f"agendado p/ {item['agendamento']}" if publish_at else (
            PRIVACY.get(norm(item["privacidade"]), "private"))
        print(f"  tags: {item['tags'] or '-'} | thumb: {item['thumbnail'] or '-'} | {when}")
        if args.dry_run:
            continue

        try:
            with tempfile.TemporaryDirectory() as tmp:
                vpath = Path(tmp) / video_file["name"]
                download_file(drive, video_file["id"], vpath)
                video_id = upload_video(youtube, vpath, item, publish_at)
        except HttpError as e:
            print(f"  ! erro no upload: {e}")
            if is_stop_error(e):
                print("  Cota/limite diário do YouTube esgotado. Parando; as linhas ficam pendentes.")
                break
            errors += 1
            mark(item, status="erro", observacao=f"upload: {str(e)[:250]}")
            continue
        except Exception as e:
            errors += 1
            mark(item, status="erro", observacao=f"upload: {str(e)[:250]}")
            continue

        link = f"https://youtu.be/{video_id}"
        print(f"  ✓ enviado: {link}")
        mark(item, status="enviado", link=link, observacao="")
        done += 1

        thumb_ok = True
        if thumb_file:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    tpath = Path(tmp) / thumb_file["name"]
                    download_file(drive, thumb_file["id"], tpath)
                    set_thumbnail(youtube, video_id, tpath)
                print("  ✓ thumbnail definida")
            except Exception as e:
                thumb_ok = False
                print(f"  ! thumbnail falhou: {e}")
                mark(item, observacao=f"vídeo enviado, mas a thumbnail falhou: {str(e)[:200]}")

        if not args.keep:
            remove_from_drive(drive, video_file["id"], args.permanent_delete)
            if thumb_file and thumb_ok:
                remove_from_drive(drive, thumb_file["id"], args.permanent_delete)
            print("  ✓ removido do Drive")

    print(f"\nConcluído: {done} enviado(s), {errors} com erro.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
