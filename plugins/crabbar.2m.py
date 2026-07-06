#!/usr/bin/env python3
# <xbar.title>CrabBar (Claude Code na barra de menus)</xbar.title>
# <xbar.version>v2.0</xbar.version>
# <xbar.author>Autora: Ana G. Soares (adaptado do Clawdmeter de HermannBjorgvin)</xbar.author>
# <xbar.desc>Uso do Claude Code na barra de menus: TODOS os limites (5h, semanal, por-modelo como Fable/Opus/Sonnet) lidos do endpoint de usage da Anthropic (zero token), mais gasto local via ccusage.</xbar.desc>
# <xbar.dependencies>python3,node(npx),ccusage</xbar.dependencies>
#
# Plugin SwiftBar/xbar. Intervalo no nome do arquivo (.2m.) = re-executa a cada 2 minutos.
# Só stdlib, sem venv. Ver o README do repositório.
#
# Duas fontes de dados, AMBAS custo ZERO:
#   1. Limites % (sessão 5h, semanal, por-modelo): GET /api/oauth/usage — o mesmo
#      endpoint que o `/usage` do Claude Code lê. NÃO invoca modelo → zero token, zero crédito.
#      Descobre automaticamente todo limite que a Anthropic reportar (Fable, Opus, Sonnet, ...).
#   2. Gasto (tokens + $): `ccusage -j` lê ~/.claude/projects/**/*.jsonl localmente.
#
# Env opcionais:
#   CRAB_NO_PING=1     desliga a leitura de limites (fica só o gasto local)
#   CRAB_SHOW=remaining mostra % RESTANTE em vez de % usado
#   CRAB_WARN=75        threshold âmbar (default 75)
#   CRAB_CRIT=90        threshold vermelho (default 90)

import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
API_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "User-Agent": "claude-code/2.1.5",
}

# Resiliência: cache do último valor bom + backoff em 429 (estilo LimitBar).
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "crabbar")
CACHE_FILE = os.path.join(CACHE_DIR, "usage.json")
STALE_MAX_MIN = 60       # acima disso o cache é velho demais → indisponível
BACKOFF_MIN_429 = 10     # após 429, não bate na API por N min (serve cache)
FETCH_RETRIES = 2        # tentativas extras por execução em erro transitório

# Thresholds (% usado) para a cor. Configuráveis por env.
def _thr(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

WARN = _thr("CRAB_WARN", 75)
CRIT = _thr("CRAB_CRIT", 90)
SHOW_REMAINING = os.environ.get("CRAB_SHOW", "").lower() in ("remaining", "restante", "left")

# Cores. Pares "claro,escuro": 1º p/ tema claro (fundo branco → tons escuros),
# 2º p/ tema escuro (tons mais vivos).
GREEN = "#6f42c1,#c9a8ff"  # roxo (nível "ok", baixo uso)
AMBER = "#9a6700,#e3b341"  # âmbar (>= WARN)
RED = "#cf222e,#ff7b72"    # vermelho (>= CRIT)
DIM = "#57606a,#9aa4af"    # cinza de apoio (cabeçalhos, status)


def color_for(used_pct):
    """Cor sempre baseada no % USADO (mesmo quando exibimos o restante)."""
    if used_pct is None:
        return DIM
    if used_pct >= CRIT:
        return RED
    if used_pct >= WARN:
        return AMBER
    return GREEN


# ------------------------------------------------- rótulos dos escopos de limite
# Ordem de exibição e rótulos amigáveis. Chaves desconhecidas viram rótulo
# derivado do próprio nome (auto-descoberta estilo LimitBar).
SCOPE_ORDER = [
    "five_hour", "seven_day",
    "seven_day_opus", "seven_day_sonnet", "seven_day_fable", "seven_day_haiku",
    "seven_day_overage_included",
]
SCOPE_LABEL = {
    "five_hour": "Sessão (5h)",
    "seven_day": "Semanal · todos os modelos",
    "seven_day_opus": "Semanal · Opus",
    "seven_day_sonnet": "Semanal · Sonnet",
    "seven_day_fable": "Semanal · Fable",
    "seven_day_haiku": "Semanal · Haiku",
    "seven_day_overage_included": "Semanal · com overage",
}
SCOPE_SHORT = {
    "five_hour": "5h",
    "seven_day": "7d",
    "seven_day_opus": "Opus",
    "seven_day_sonnet": "Sonnet",
    "seven_day_fable": "Fable",
    "seven_day_haiku": "Haiku",
    "seven_day_overage_included": "Over",
}


def label_for(key):
    if key in SCOPE_LABEL:
        return SCOPE_LABEL[key]
    pretty = key.replace("seven_day_", "").replace("seven_day", "semanal")
    pretty = pretty.replace("five_hour", "sessão").replace("_", " ").strip()
    return f"Semanal · {pretty.title()}" if key.startswith("seven_day_") else pretty.title()


def short_for(key):
    if key in SCOPE_SHORT:
        return SCOPE_SHORT[key]
    return key.replace("seven_day_", "").replace("five_hour", "5h")[:6].title()


def order_key(key):
    return (SCOPE_ORDER.index(key) if key in SCOPE_ORDER else 99, key)


# ---------------------------------------------------------------- spend (free)
def run_ccusage():
    """Return ccusage JSON dict, or None. Tries global `ccusage` then `npx`."""
    candidates = [
        ["ccusage", "-j"],
        ["npx", "--yes", "ccusage@latest", "-j"],
    ]
    override = os.environ.get("CRAB_CCUSAGE_CMD")
    if override:
        candidates.insert(0, override.split())
    env = dict(os.environ)
    # npx/node need a sane PATH when launched from SwiftBar's minimal env.
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    for cmd in candidates:
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, env=env
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if out.returncode != 0 or not out.stdout.strip():
            continue
        try:
            return json.loads(out.stdout)
        except json.JSONDecodeError:
            continue
    return None


def parse_spend(data):
    """Extract today's and all-time spend from ccusage JSON."""
    if not data:
        return None
    today = time.strftime("%Y-%m-%d")
    daily = data.get("daily") or []
    day = next((d for d in daily if d.get("period") == today), None)
    if day is None and daily:
        day = daily[-1]  # fall back to most recent day present
    totals = data.get("totals") or {}
    day = day or {}

    def io(d):  # "novo": trabalho real = input + output
        return d.get("inputTokens", 0) + d.get("outputTokens", 0)

    def cache(d):  # cache write + read (releitura de contexto, infla o total)
        return d.get("cacheCreationTokens", 0) + d.get("cacheReadTokens", 0)

    return {
        "today_tok": day.get("totalTokens", 0),
        "today_cost": day.get("totalCost", 0.0),
        "today_io": io(day),
        "today_cache": cache(day),
        "today_is_today": bool(day and day.get("period") == today),
        "all_tok": totals.get("totalTokens", 0),
        "all_cost": totals.get("totalCost", 0.0),
        "all_io": io(totals),
        "all_cache": cache(totals),
    }


# ---------------------------------------------------------- limits (zero cost)
def extract_token(blob):
    blob = (blob or "").strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def read_token():
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", getpass.getuser(), "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return extract_token(out.stdout)


# ---- auto-refresh do token OAuth (OPT-IN: CRAB_AUTO_REFRESH=1) --------------
# Regrava o MESMO item de Keychain que o Claude Code usa. Por isso é opt-in,
# faz backup antes de escrever e preserva todos os campos existentes.
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
DEFAULT_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # client_id público do Claude Code


def _read_credentials():
    """(data_dict_completo, oauth_subdict) do Keychain, ou None. Preserva o shape."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", getpass.getuser(), "-w"],
            capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = data if "accessToken" in data else None
    if not isinstance(oauth, dict):
        return None
    return data, oauth


def _write_credentials(data):
    """Regrava o blob no Keychain (backup antes). True se ok."""
    try:
        blob = json.dumps(data)
    except (TypeError, ValueError):
        return False
    try:  # backup local pra recuperação manual se algo der errado
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, "creds-backup.json"), "w") as f:
            f.write(blob)
    except OSError:
        pass
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", KEYCHAIN_SERVICE, "-a", getpass.getuser(), "-w", blob],
            capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def refresh_token(now):
    """Renova o access token via OAuth e regrava. Retorna o novo token ou None.

    Só roda com CRAB_AUTO_REFRESH=1. Mesmo fluxo que o Claude Code usa:
    POST /v1/oauth/token grant_type=refresh_token. NÃO gasta token de modelo.
    """
    if os.environ.get("CRAB_AUTO_REFRESH") != "1":
        return None
    creds = _read_credentials()
    if not creds:
        return None
    data, oauth = creds
    rt = oauth.get("refreshToken")
    if not rt:
        return None
    client_id = os.environ.get("CLAUDE_CODE_OAUTH_CLIENT_ID", DEFAULT_CLIENT_ID)
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": API_HEADERS["User-Agent"]})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        r = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    at = r.get("access_token")
    exp = r.get("expires_in")
    if not isinstance(at, str) or not isinstance(exp, (int, float)):
        return None
    oauth["accessToken"] = at
    oauth["refreshToken"] = r.get("refresh_token", rt)
    oauth["expiresAt"] = int(now * 1000) + int(exp) * 1000
    _write_credentials(data)
    return at


def _as_pct(util):
    """utilization do /api/oauth/usage vem em 0–100 (confirmado)."""
    try:
        return int(round(float(util)))
    except (TypeError, ValueError):
        return None


def _mins_until(resets_at):
    """resets_at (ISO 8601, ou epoch em segundos) → minutos até o reset."""
    if resets_at in (None, ""):
        return None
    if isinstance(resets_at, str):
        try:
            dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
            delta = (dt - datetime.now(timezone.utc)).total_seconds() / 60.0
            return int(round(delta)) if delta > 0 else 0
        except ValueError:
            pass
    try:  # epoch segundos
        delta = (float(resets_at) - time.time()) / 60.0
        return int(round(delta)) if delta > 0 else 0
    except (TypeError, ValueError):
        return None


def _collect_scopes(obj, out):
    """Acha recursivamente todo objeto {utilization, resets_at} e guarda pela chave-pai."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict) and "utilization" in v:
                out[k] = v
            else:
                _collect_scopes(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_scopes(v, out)


def _read_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(obj):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _fetch_usage_once(token):
    """(status_code|None, body_bytes). status None = erro de rede/offline."""
    headers = dict(API_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(USAGE_URL, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        return 200, resp.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
    except (urllib.error.URLError, OSError):
        return None, b""


def _parse_usage_body(body):
    """Return sorted list of limit dicts, or None."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, AttributeError):
        return None
    scopes = {}
    _collect_scopes(data, scopes)
    limits = []
    for key, val in scopes.items():
        used = _as_pct(val.get("utilization"))
        if used is None:
            continue
        limits.append({
            "key": key,
            "label": label_for(key),
            "short": short_for(key),
            "used": used,
            "reset": _mins_until(val.get("resets_at")),
        })
    if not limits:
        return None
    limits.sort(key=lambda l: order_key(l["key"]))
    return limits


def _stale_result(cache, now, note):
    """Serve o último valor bom (se recente o bastante), senão None."""
    if not cache or not cache.get("limits"):
        return None
    age = int(round((now - cache.get("ts", now)) / 60))
    if age > STALE_MAX_MIN:
        return None
    return {"limits": cache["limits"], "stale": True, "age_min": max(0, age), "note": note}


def poll_limit():
    """Return {limits, stale, age_min, note} from /api/oauth/usage, or None.

    Zero token. Guarda o último valor bom em cache; em falha transitória
    (offline, 429, 5xx) serve o cache marcado como 'stale' em vez de sumir.
    Em 429 sustentado, recua BACKOFF_MIN_429 min antes de bater de novo.
    """
    if os.environ.get("CRAB_NO_PING") == "1":
        return None
    now = time.time()
    cache = _read_cache()

    # Respeita o backoff: não bate na API enquanto a janela de 429 não passar.
    if cache and cache.get("backoff_until", 0) > now:
        return _stale_result(cache, now, note="backoff 429")

    token = read_token()
    if not token:
        return _stale_result(cache, now, note="sem token")

    transient = {429, 500, 502, 503, 504}
    status, body = None, b""
    for attempt in range(FETCH_RETRIES + 1):
        status, body = _fetch_usage_once(token)
        if status == 200 or (status is not None and status not in transient):
            break
        if attempt < FETCH_RETRIES:
            time.sleep(1.5 * (attempt + 1))  # backoff curto dentro da execução

    # Token expirado? Renova (opt-in) e tenta de novo uma vez.
    if status in (401, 403):
        new = refresh_token(now)
        if new:
            status, body = _fetch_usage_once(new)

    if status == 200:
        limits = _parse_usage_body(body)
        if limits:
            _write_cache({"ts": now, "limits": limits, "backoff_until": 0})
            return {"limits": limits, "stale": False, "age_min": 0, "note": ""}
        return _stale_result(cache, now, note="resposta vazia")

    # Falha: em 429 sustentado, arma o backoff; sempre serve o cache se houver.
    if status == 429 and cache:
        cache["backoff_until"] = now + BACKOFF_MIN_429 * 60
        _write_cache(cache)
    return _stale_result(cache, now, note=("429" if status == 429 else "offline"))


# ----------------------------------------------------------------- formatting
def fmt_tokens(n):
    n = n or 0
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def fmt_mins(m):
    if m is None:
        return "?"
    if m >= 1440:  # >= 24h → dias + horas
        return f"{m // 1440}d {(m % 1440) // 60}h"
    if m >= 60:
        return f"{m // 60}h {m % 60}m"
    return f"{m}m"


def disp_pct(used):
    """% a exibir conforme CRAB_SHOW (usado por default, restante se pedido)."""
    if used is None:
        return None
    return 100 - used if SHOW_REMAINING else used


# Texto principal forte (legível nos dois temas) e cabeçalho de seção.
TEXT = "#1f2328,#e6edf3"          # quase-preto no claro / quase-branco no escuro
FONT = f"size=14 font=Menlo color={TEXT}"   # linhas de dado
HEAD = f"size=12 color={DIM}"               # cabeçalhos de seção


def main():
    spend = parse_spend(run_ccusage())
    lim = poll_limit()
    limits = lim["limits"] if lim else None
    stale = bool(lim and lim["stale"])
    age = lim["age_min"] if lim else 0

    # ----- menu bar title -----
    TITLE = "#ffffff"  # branco puro (sem par claro/escuro)
    if limits:
        worst = max((l["used"] for l in limits), default=0)
        flag = "🔴" if worst >= CRIT else ("🟠" if worst >= WARN else "")
        tail = " ⏳" if stale else ""  # ⏳ = número de cache (offline/backoff)
        parts = [f"{l['short']} {disp_pct(l['used'])}%" for l in limits]
        print(f"🦀{flag} " + " · ".join(parts) + tail + f" | color={TITLE}")
    elif spend:
        print(f"🦀 ${spend['all_cost']:.2f} | color={TITLE}")
    else:
        print(f"🦀 – | color={TITLE}")

    print("---")

    # ----- limits section -----
    mode = "restante" if SHOW_REMAINING else "usado"
    if limits:
        hdr = f"Limites (% {mode})"
        if stale:
            hdr += f" · ⏳ cache de {age}m atrás ({lim['note']})"
        print(f"{hdr} | {HEAD}")
        for l in limits:
            shown = disp_pct(l["used"])
            # se stale, o reset foi calculado há `age` min → ajusta.
            rem = l["reset"]
            if rem is not None and stale:
                rem = max(0, rem - age)
            reset = f"reset em {fmt_mins(rem)}" if rem is not None else ""
            color = DIM if stale else color_for(l["used"])
            print(f"{l['label']}:  {shown}%   {reset} | "
                  f"size=14 font=Menlo color={color}")
        src = "último valor bom (offline)" if stale else "/api/oauth/usage — zero token"
        print(f"Fonte: {src} | {HEAD}")
    elif os.environ.get("CRAB_NO_PING") == "1":
        print(f"Limites: leitura desligada (CRAB_NO_PING=1) | {HEAD}")
    else:
        print(f"Limites indisponíveis (sem token ou offline) | {HEAD}")

    print("---")

    # ----- spend section (free, from ccusage) -----
    if spend:
        label = "Hoje" if spend["today_is_today"] else "Último dia"
        print(f"Gasto (local, custo zero) | {HEAD}")
        print(f"{label}:  {fmt_tokens(spend['today_tok'])} tok   "
              f"${spend['today_cost']:.2f} | {FONT}")
        print(f"  ↳ novo (in+out): {fmt_tokens(spend['today_io'])}   "
              f"cache: {fmt_tokens(spend['today_cache'])} | {HEAD} font=Menlo")
        print(f"Total: {fmt_tokens(spend['all_tok'])} tok   "
              f"${spend['all_cost']:.2f} | {FONT}")
        print(f"  ↳ novo (in+out): {fmt_tokens(spend['all_io'])}   "
              f"cache: {fmt_tokens(spend['all_cache'])} | {HEAD} font=Menlo")
    else:
        print(f"Gasto indisponível (ccusage falhou) | {HEAD}")
        print(f"Instalar: npm i -g ccusage | {HEAD}")

    print("---")
    print(f"Atualizado {time.strftime('%H:%M:%S')} | {HEAD}")
    print("Atualizar agora | refresh=true")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break the menu bar
        print("🦀 ⚠️")
        print("---")
        print(f"Erro: {e} | color={RED} size=11")
        print("Atualizar agora | refresh=true")
        sys.exit(0)
