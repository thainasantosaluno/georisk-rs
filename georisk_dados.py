"""
GeoRisk-RS — MOTOR DE DADOS REAIS
=================================
Este módulo é o único lugar do projeto que fala com a internet e com o banco.
`main.py` e `dashboard.py` apenas leem daqui — nenhum dos dois inventa valor.

FONTES REAIS (todas verificadas e em HTML/CSV/XML público, sem token):

  1) SACE / SGB-CPRM  ..........  https://www.sgb.gov.br/sace/sace_nivel/
     - `estacoes_mapa.php?bacia=X` é uma página Leaflet cujo HTML embute os
       marcadores: `L.marker([lat, lon], {icon: <Status>})` + o tooltip
       "codigo - Nome" + o link do relatório. Também embute o array
       `pontosChuva` com a chuva acumulada em 24 h de cada ponto.
     - `relatorio.php?...` traz o NÍVEL ATUAL, a data/hora da medição, a
       SITUAÇÃO oficial e — o mais importante — as COTAS OFICIAIS de
       atenção / alerta / inundação daquela estação.
     - `api/dados/<bacia>_<pm>_cota.csv` e `_chuva.csv` trazem a série real
       dos últimos ~30 dias em passo de 15 minutos.
     Bacias do RS com dado publicado: taquari, cai, guaiba, uruguai.

  2) ANA — Telemetria  .........  https://telemetriaws1.ana.gov.br/ServiceANA.asmx
     - `ListaEstacoesTelemetricas` devolve XML com todas as estações
       telemétricas do país; filtramos `Municipio-UF` terminado em "-RS".
     - `DadosHidrometeorologicos?codEstacao=&dataInicio=&dataFim=` devolve
       XML com Nivel / Vazao / Chuva por horário.

FONTES AVALIADAS E DESCARTADAS (documentado de propósito, para ninguém
"consertar" isso com número fictício depois):

  - INMET: a lista de estações (`apitempo.inmet.gov.br/estacoes/T`) responde
    normalmente, mas TODOS os endpoints de leitura hoje devolvem 204/404 e o
    portal `tempo.inmet.gov.br` está atrás de proteção anti-bot. Sem leitura
    real disponível, a estação NÃO entra — preferimos 0 estação INMET a uma
    estação INMET com número inventado. Se você conseguir um token da API do
    INMET, o ponto de extensão é a função `coletar_inmet()` no fim do arquivo.
  - CEMADEN: não há endpoint público aberto (o mapa interativo exige sessão).

REGRA DE OURO DO PROJETO: nenhum valor exibido é sintético. Onde a fonte não
publica, o campo fica NULL e a interface mostra "Sem dado" / cinza.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# -----------------------------------------------------------------------------
# CONFIGURAÇÃO
# -----------------------------------------------------------------------------
CAMINHO_BANCO = Path(__file__).resolve().parent / "georisk_rs.db"

SACE_BASE = "https://www.sgb.gov.br/sace/sace_nivel/"
ANA_BASE = "https://telemetriaws1.ana.gov.br/ServiceANA.asmx/"

# Bacias do RS que o SACE publica. Chave = parâmetro da URL; valor = rótulo.
BACIAS_SACE = {
    "taquari": "Bacia do Taquari-Antas",
    "cai": "Bacia do Rio Caí",
    "guaiba": "Bacia do Guaíba",
    "uruguai": "Bacia do Rio Uruguai",
}

# Ícone do marcador no HTML do SACE -> (situação legível, cor no mapa).
# São as cores oficiais do próprio SACE.
ICONE_PARA_SITUACAO = {
    "Normal": ("Normal", "green"),
    "CotaDeAteno": ("Cota de Atenção", "gold"),
    "CotaDeAlerta": ("Cota de Alerta", "orange"),
    "CotaDeInundao": ("Cota de Inundação", "red"),
    "CotaDeInundaoSevera": ("Cota de Inundação Severa", "purple"),
    "CotaDeSecaModerada": ("Seca Moderada", "purple"),
    "CotaDeSecaExcepcional": ("Seca Excepcional", "red"),
    "SemTransmisso": ("Sem transmissão", "gray"),
    "EstaoSemTransmisso": ("Sem transmissão", "gray"),
}

TEMPO_LIMITE = 60

# Repetição em falha transitória de rede. Ver `_baixar()` para o motivo.
TENTATIVAS = 3
ESPERA_INICIAL = 2.0   # segundos; dobra a cada tentativa (2, 4, 8…)
_local = threading.local()


def _sessao() -> requests.Session:
    """Uma Session por thread (requests.Session não é thread-safe)."""
    s = getattr(_local, "sessao", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (GeoRisk-RS/monitoramento)"})
        s.verify = False
        _local.sessao = s
    return s


def _baixar(url: str, tentativas: int = TENTATIVAS, espera: float = ESPERA_INICIAL) -> str:
    """GET com detecção de charset e REPETIÇÃO em falha transitória.

    Por que existe a repetição: as duas primeiras execuções agendadas no
    GitHub Actions falharam com o MESMO código que depois rodou três vezes
    seguidas sem alteração nenhuma. Ou seja, foi indisponibilidade momentânea
    do SGB ou da ANA — servidores que já recusaram conexão durante o
    desenvolvimento.

    O coletor já tolera uma fonte individual cair (registra em `erros` e
    segue), mas a primeira requisição de cada fonte é fatal: sem a lista de
    bacias ou o cadastro da ANA não há o que coletar, e a rodada inteira se
    perde. Três tentativas com espera crescente cobrem a falha passageira sem
    mascarar indisponibilidade real — se a fonte estiver mesmo fora, o erro
    sobe igual, só que depois de ter dado chance.

    Não repete em erro 4xx: se o servidor diz que a URL não existe ou que o
    acesso é proibido, insistir não muda nada e só gasta tempo.
    """
    ultimo_erro: Exception | None = None

    for tentativa in range(1, tentativas + 1):
        try:
            r = _sessao().get(url, timeout=TEMPO_LIMITE)
            # 4xx é definitivo: não adianta insistir.
            if 400 <= r.status_code < 500:
                r.raise_for_status()
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code} em {url}")
            m = re.search(r"charset=([\w-]+)", r.headers.get("content-type", ""), re.I)
            r.encoding = m.group(1) if m else "utf-8"
            return r.text
        except requests.HTTPError as exc:
            resposta = getattr(exc, "response", None)
            if resposta is not None and 400 <= resposta.status_code < 500:
                raise
            ultimo_erro = exc
        except (requests.ConnectionError, requests.Timeout, OSError) as exc:
            ultimo_erro = exc

        if tentativa < tentativas:
            time.sleep(espera * (2 ** (tentativa - 1)))  # 2 s, 4 s, 8 s…

    raise ultimo_erro if ultimo_erro else RuntimeError(f"Falha ao baixar {url}")


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _float(txt) -> float | None:
    """Float de dado técnico (ponto decimal). Vazio/None -> None."""
    if txt is None:
        return None
    txt = str(txt).strip()
    if not txt:
        return None
    try:
        return float(txt)
    except ValueError:
        return None


def _float_ptbr(txt) -> float | None:
    """Float de número exibido em pt-BR: '1.193' = 1193 ; '1.193,5' = 1193,5."""
    if txt is None:
        return None
    txt = str(txt).strip()
    if not txt:
        return None
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    else:
        txt = txt.replace(".", "")
    try:
        return float(txt)
    except ValueError:
        return None


# -----------------------------------------------------------------------------
# BANCO (SQLite — o mesmo georisk_rs.db do projeto)
# -----------------------------------------------------------------------------
def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(CAMINHO_BANCO, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def criar_schema() -> None:
    """Cria as tabelas do monitoramento real.

    Não mexe na tabela antiga `leituras_estacoes` (a que continha os valores
    fictícios): ela é simplesmente ignorada. Se quiser limpá-la, rode uma vez
    `DROP TABLE leituras_estacoes;` — nada aqui depende dela.
    """
    with conectar() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS estacao (
                id                 TEXT PRIMARY KEY,
                fonte              TEXT,
                codigo             TEXT,
                nome               TEXT,
                municipio          TEXT,
                rio                TEXT,
                bacia              TEXT,
                lat                REAL,
                lon                REAL,
                tipo               TEXT,
                automatica         INTEGER DEFAULT 1,
                cota_atencao_cm    REAL,
                cota_alerta_cm     REAL,
                cota_inundacao_cm  REAL,
                nivel_cm           REAL,
                vazao_m3s          REAL,
                chuva_1h           REAL,
                chuva_24h          REAL,
                chuva_72h          REAL,
                medido_em          TEXT,
                situacao           TEXT,
                cor                TEXT,
                observacao         TEXT,   -- por que algum campo ficou NULL
                url_origem         TEXT,
                atualizado_em      TEXT
            );

            CREATE TABLE IF NOT EXISTS serie (
                id_estacao TEXT,
                grandeza   TEXT,          -- 'cota' | 'chuva'
                datahora   TEXT,          -- 'YYYY-MM-DD HH:MM:SS'
                valor      REAL,
                unidade    TEXT,          -- 'cm' | 'mm'
                PRIMARY KEY (id_estacao, grandeza, datahora)
            );

            CREATE TABLE IF NOT EXISTS coleta (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                iniciada_em  TEXT,
                terminada_em TEXT,
                fontes       TEXT,
                estacoes     INTEGER,
                erros        INTEGER,
                detalhe      TEXT
            );

            CREATE INDEX IF NOT EXISTS ix_serie ON serie (id_estacao, grandeza, datahora);
            CREATE INDEX IF NOT EXISTS ix_estacao_bacia ON estacao (bacia);
            """
        )
        # Migração leve: bancos criados antes da padronização não têm essas colunas.
        colunas = {linha[1] for linha in con.execute("PRAGMA table_info(serie)")}
        if "unidade" not in colunas:
            con.execute("ALTER TABLE serie ADD COLUMN unidade TEXT")
        colunas = {linha[1] for linha in con.execute("PRAGMA table_info(estacao)")}
        if "observacao" not in colunas:
            con.execute("ALTER TABLE estacao ADD COLUMN observacao TEXT")


# -----------------------------------------------------------------------------
# COLETA 1 — SACE / SGB  (nível + COTAS OFICIAIS + chuva + série 15 min)
# -----------------------------------------------------------------------------
RE_MARCADOR = re.compile(
    r"relatorio\.php\?apenas_grafico=sim&bacia=(\w+)&pm=(\d*)&s=(\d*)&sr=(\d*)"
    r".*?L\.marker\(\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]\s*,\s*\{icon:\s*(\w+)\}"
    r".*?bindTooltip\(\s*\"([^\"]*)\"",
    re.S,
)
RE_PONTOS_CHUVA = re.compile(r"const\s+pontosChuva\s*=\s*(\[.*?\]);", re.S)

RE_NIVEL = re.compile(r"<h2[^>]*>\s*([\d.,]+)\s*Cota\s*\(cm\)\s*</h2>", re.I)
RE_MEDICAO = re.compile(r"medi[çc][ãa]o:\s*<b>\s*([^<]+?)\s*</b>", re.I)
RE_SITUACAO = re.compile(r"Situa[çc][ãa]o:\s*<span[^>]*>\s*</span>\s*([^<]+?)\s*<br", re.I)
RE_RIO = re.compile(r"N[íi]vel do rio\s+(.+?)\s+e altura de chuva\s*-\s*(.+?)\s*<br", re.I)
# Fallback: em algumas estações a linha sai só como "... chuva - Nome<br".
RE_LOCAL = re.compile(r"chuva\s*-\s*([^<]+?)\s*<br", re.I)

# Valor-sentinela que o SACE usa para "sem medição" em alguns CSVs.
SENTINELA = 9999.0


def _re_cota(rotulo: str) -> re.Pattern:
    """O SACE alterna 'Cota de Atenção' e 'Cota de atenção' entre páginas."""
    return re.compile(
        r"label:\s*'Cota de\s+" + rotulo + r"'.*?\(\)\s*=>\s*([\d.]+)\)", re.I | re.S
    )


RE_COTA_ATENCAO = _re_cota(r"aten[çc][ãa]o")
RE_COTA_ALERTA = _re_cota(r"alerta")
RE_COTA_INUNDACAO = _re_cota(r"inunda[çc][ãa]o")


def _limpar_nome(bruto: str) -> tuple[str, str]:
    """'8672000 - Encantado' -> ('8672000', 'Encantado').
    Também remove sufixos operacionais do SACE (' - WEB', ' Aux.')."""
    partes = bruto.split(" - ", 1)
    if len(partes) == 2 and partes[0].strip().isdigit():
        codigo, nome = partes[0].strip(), partes[1].strip()
    else:
        codigo, nome = "", bruto.strip()
    nome = re.sub(r"\s*-\s*WEB\s*$", "", nome, flags=re.I)
    nome = re.sub(r"\s+Aux\.?\s*$", "", nome, flags=re.I)
    return codigo, nome.strip()


def _mapa_sace(bacia: str) -> list[dict]:
    """Lê a página Leaflet de uma bacia e devolve as estações com o que já
    dá para saber sem abrir o relatório (posição, status, chuva 24 h)."""
    html = _baixar(f"{SACE_BASE}estacoes_mapa.php?bacia={bacia}")

    chuva_por_ponto: dict[tuple[str, str], float] = {}
    m = RE_PONTOS_CHUVA.search(html)
    if m:
        try:
            for p in json.loads(m.group(1)):
                # p = [lat, lon, "codigo - nome", "chuva_24h"]
                chuva_por_ponto[(str(p[0]), str(p[1]))] = _float(p[3])
        except (ValueError, IndexError, TypeError):
            chuva_por_ponto = {}

    estacoes = []
    for bac, pm, s, sr, lat, lon, icone, tooltip in RE_MARCADOR.findall(html):
        codigo, nome = _limpar_nome(tooltip)
        situacao, cor = ICONE_PARA_SITUACAO.get(icone, ("Sem classificação", "gray"))
        estacoes.append(
            {
                # A chave usa a bacia REAL do relatório (a página 'guaiba'
                # reexibe estações de 'cai'/'taquari'; isso deduplica sozinho).
                "id": f"SACE_{bac}_{pm}_{s}_{sr}",
                "fonte": "SACE/SGB",
                "codigo": codigo,
                "nome": nome,
                "municipio": nome,
                "bacia": BACIAS_SACE.get(bac, BACIAS_SACE.get(bacia, "Não informada")),
                "lat": _float(lat),
                "lon": _float(lon),
                "automatica": 1,
                "situacao": situacao,
                "cor": cor,
                "chuva_24h": chuva_por_ponto.get((lat, lon)),
                "_bacia_url": bac,
                "_pm": pm,
                "_s": s,
                "_sr": sr,
                "url_origem": (
                    f"{SACE_BASE}relatorio.php?apenas_grafico=sim"
                    f"&bacia={bac}&pm={pm}&s={s}&sr={sr}"
                ),
            }
        )
    return estacoes


def _relatorio_sace(est: dict) -> dict:
    """Abre o relatório da estação e extrai nível, data/hora, rio e as COTAS
    OFICIAIS. É daqui que sai o limiar de risco — nunca de tabela chutada."""
    html = _baixar(est["url_origem"])
    dados: dict = {}

    m = RE_NIVEL.search(html)
    if m:
        dados["nivel_cm"] = _float_ptbr(m.group(1))
        dados["tipo"] = "FLUVIOMETRICA"
    else:
        # Sem "Cota (cm)" no cabeçalho => posto só de chuva.
        dados["tipo"] = "PLUVIOMETRICA"

    m = RE_MEDICAO.search(html)
    if m:
        try:
            dados["medido_em"] = datetime.strptime(
                m.group(1).strip(), "%d/%m/%Y %H:%M"
            ).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            dados["medido_em"] = m.group(1).strip()

    m = RE_SITUACAO.search(html)
    if m:
        # A situação escrita pelo próprio SGB tem prioridade sobre o ícone.
        dados["situacao"] = m.group(1).strip()

    m = RE_RIO.search(html)
    if m:
        dados["rio"] = m.group(1).strip()
        dados["municipio"] = m.group(2).strip()
    else:
        m = RE_LOCAL.search(html)
        if m:
            dados["municipio"] = m.group(1).strip()

    for chave, padrao in (
        ("cota_atencao_cm", RE_COTA_ATENCAO),
        ("cota_alerta_cm", RE_COTA_ALERTA),
        ("cota_inundacao_cm", RE_COTA_INUNDACAO),
    ):
        m = padrao.search(html)
        if m:
            dados[chave] = _float(m.group(1))

    return dados


def _serie_sace(bacia_url: str, pm: str, grandeza: str) -> pd.DataFrame:
    """Série real de 30 dias em passo de 15 min, direto do CSV do SACE."""
    url = f"{SACE_BASE}api/dados/{bacia_url}_{pm}_{grandeza}.csv"
    try:
        texto = _baixar(url)
    except Exception:
        return pd.DataFrame(columns=["datahora", "valor"])

    linhas = []
    for linha in texto.strip().splitlines()[1:]:
        partes = linha.split(";")
        if len(partes) != 2:
            continue
        valor = _float(partes[1])
        # 9999 é o "sem medição" do SACE; negativo em chuva é ruído de sensor.
        if valor is None or valor >= SENTINELA:
            continue
        if grandeza == "chuva" and valor < 0:
            continue
        linhas.append((partes[0].strip(), valor))
    return pd.DataFrame(linhas, columns=["datahora", "valor"])


def _chuva_plausivel(serie: pd.DataFrame) -> bool:
    """Guarda de qualidade contra erro DA FONTE.

    Verificamos na prática que, para algumas estações, o arquivo
    `<bacia>_<pm>_chuva.csv` do SGB vem preenchido com a série de NÍVEL
    (valores de 200 a 1500 variando suavemente) em vez da chuva. Somar aquilo
    produzia "25.281 mm em 24 h".

    Série de chuva real é dominada por zeros: a média por passo de 15 min fica
    na casa de 0,2 mm. Se a média passar de 5 mm/passo, o arquivo não é chuva —
    descartamos em vez de exibir um número impossível.
    """
    if serie.empty:
        return False
    valores = serie["valor"]
    return bool(valores.mean() <= 5.0 and valores.max() <= 300.0)


def _acumulados(serie_chuva: pd.DataFrame) -> dict:
    """Acumulados 1 h / 24 h / 72 h somados da série real de 15 min.
    Conferido contra o valor de 24 h que o próprio SACE publica: bate."""
    if serie_chuva.empty:
        return {}
    d = serie_chuva.copy()
    d["dt"] = pd.to_datetime(d["datahora"], errors="coerce")
    d = d.dropna(subset=["dt"])
    if d.empty:
        return {}
    fim = d["dt"].max()
    return {
        f"chuva_{h}h": round(float(d.loc[d["dt"] > fim - timedelta(hours=h), "valor"].sum()), 2)
        for h in (1, 24, 72)
    }


def coletar_sace(baixar_series: bool = True, trabalhadores: int = 8) -> tuple[list[dict], list[str]]:
    """Coleta completa do SACE. Devolve (estações, erros)."""
    erros: list[str] = []
    brutas: dict[str, dict] = {}

    for bacia in BACIAS_SACE:
        try:
            for est in _mapa_sace(bacia):
                brutas.setdefault(est["id"], est)  # dedup entre bacias
        except Exception as exc:
            erros.append(f"mapa {bacia}: {type(exc).__name__}: {exc}")

    estacoes = list(brutas.values())

    def _detalhar(est: dict) -> dict:
        try:
            est.update(_relatorio_sace(est))
        except Exception as exc:
            erros.append(f"relatorio {est['id']}: {type(exc).__name__}")
        if baixar_series:
            try:
                cota = _serie_sace(est["_bacia_url"], est["_pm"], "cota")
                chuva = _serie_sace(est["_bacia_url"], est["_pm"], "chuva")

                if not cota.empty:
                    est["_serie_cota"] = cota
                    est["tipo"] = "FLUVIOMETRICA"
                    # O cabeçalho HTML às vezes vem sem o número (constatado na
                    # estação Estrela). O CSV é da mesma fonte e é o que o
                    # próprio SACE plota, então serve de fallback confiável.
                    if est.get("nivel_cm") is None:
                        est["nivel_cm"] = float(cota["valor"].iloc[-1])
                        if not est.get("medido_em"):
                            est["medido_em"] = str(cota["datahora"].iloc[-1])

                if _chuva_plausivel(chuva):
                    est["_serie_chuva"] = chuva
                    est.update(_acumulados(chuva))
                else:
                    # Mantemos a chuva de 24 h que o SACE publica no mapa
                    # (array pontosChuva), que é independente do CSV suspeito,
                    # e apagamos qualquer série ruim já gravada antes.
                    est["_purgar_chuva"] = True
                    erros.append(f"chuva implausivel descartada: {est['id']}")
            except Exception as exc:
                erros.append(f"serie {est['id']}: {type(exc).__name__}")
        return est

    with ThreadPoolExecutor(max_workers=trabalhadores) as ex:
        estacoes = list(ex.map(_detalhar, estacoes))

    return estacoes, erros


# -----------------------------------------------------------------------------
# COLETA 2 — ANA / Telemetria  (estações automáticas de todo o RS)
# -----------------------------------------------------------------------------
def _xml_registros(texto: str, tag: str) -> list[dict]:
    raiz = ET.fromstring(texto)
    saida = []
    for elemento in raiz.iter():
        if elemento.tag.split("}")[-1] == tag:
            saida.append({f.tag.split("}")[-1]: f.text for f in elemento})
    return saida


_cadastro_nacional: list[dict] | None = None


def _cadastro_ana_nacional() -> list[dict]:
    """Cadastro telemétrico nacional da ANA (~5.2 mil estações), baixado uma
    única vez por execução. Além de listar as estações, serve de GAZETTEER:
    é ele que diz a UF de cada coordenada."""
    global _cadastro_nacional
    if _cadastro_nacional is None:
        texto = _baixar(f"{ANA_BASE}ListaEstacoesTelemetricas?statusEstacoes=&origem=")
        _cadastro_nacional = _xml_registros(texto, "Table")
    return _cadastro_nacional


_gazetteer: list[tuple[float, float, str]] | None = None


def _gazetteer_uf() -> list[tuple[float, float, str]]:
    """(lat, lon, UF) de todas as estações do cadastro nacional.
    Montado uma vez só — é consultado uma vez por estação coletada."""
    global _gazetteer
    if _gazetteer is None:
        pontos = []
        for e in _cadastro_ana_nacional():
            lat, lon = _float(e.get("Latitude")), _float(e.get("Longitude"))
            uf = (e.get("Municipio-UF") or "").strip().upper()[-2:]
            if lat is not None and lon is not None and len(uf) == 2:
                pontos.append((lat, lon, uf))
        _gazetteer = pontos
    return _gazetteer


def _uf_da_coordenada(lat: float, lon: float, raio_km: float = 2.0) -> str | None:
    """UF pela estação oficial mais próxima, se estiver bem em cima.

    Precisa existir porque o SACE não publica UF, e a bacia do Uruguai que ele
    monitora tem estações de Santa Catarina misturadas às do RS (Barra do
    Chapecó, Itapiranga, Tangará, Vila Canoas e outras). Verificado: para 8 das
    9 estações fora do RS o casamento é exato (0,00 km).
    """
    if lat is None or lon is None:
        return None
    limite = (raio_km / 111.0) ** 2
    melhor_uf, melhor_dist = None, limite
    for plat, plon, uf in _gazetteer_uf():
        dist = (plat - lat) ** 2 + (plon - lon) ** 2
        if dist < melhor_dist:
            melhor_dist, melhor_uf = dist, uf
    return melhor_uf


def _dentro_do_rs(est: dict) -> bool:
    """Mantém só o que é do Rio Grande do Sul.

    1º critério: UF do gazetteer oficial da ANA (é dado, não chute).
    2º critério (quando não há estação próxima o bastante): caixa envolvente
    do estado — usada, por exemplo, em Passo Tainhas, cuja referência mais
    próxima está a ~7 km.
    """
    lat, lon = est.get("lat"), est.get("lon")
    if lat is None or lon is None:
        return False
    uf = _uf_da_coordenada(lat, lon)
    if uf:
        return uf == "RS"
    return (
        CAIXA_RS[0] <= lat <= CAIXA_RS[1] and CAIXA_RS[2] <= lon <= CAIXA_RS[3]
    )


def _lista_estacoes_ana() -> list[dict]:
    """Estações telemétricas ativas no RS, já ordenadas por probabilidade de
    transmitir.

    Constatado na prática: os códigos de 8 dígitos iniciados por 7 ou 8 (rede
    RHN) devolvem leitura pelo serviço; os iniciados por 0 (cadastro
    complementar, em geral SEMA-RS) respondem vazio. Ordenamos os que
    respondem primeiro para que um `limite` baixo ainda traga dado útil.
    """
    rs = [
        e
        for e in _cadastro_ana_nacional()
        if (e.get("Municipio-UF") or "").strip().upper().endswith("-RS")
        and (e.get("StatusEstacao") or "").strip().lower().startswith("ativ")
    ]
    rs.sort(key=lambda e: (e.get("CodEstacao") or "").startswith("0"))
    return rs


def _dados_ana(codigo: str, dias: int = 3) -> list[dict]:
    fim = datetime.now(timezone.utc).date()
    ini = fim - timedelta(days=dias)
    url = (
        f"{ANA_BASE}DadosHidrometeorologicos?codEstacao={codigo}"
        f"&dataInicio={ini.isoformat()}&dataFim={fim.isoformat()}"
    )
    return _xml_registros(_baixar(url), "DadosHidrometereologicos")


def coletar_ana(limite: int | None = None, trabalhadores: int = 6) -> tuple[list[dict], list[str]]:
    """Estações telemétricas (automáticas) da ANA no RS, com a última leitura.

    Muitas estações do RS existem no cadastro mas não devolvem leitura pelo
    serviço; essas entram no banco com nível NULL e ficam cinza no mapa —
    de novo, sem preencher com número inventado.
    """
    erros: list[str] = []
    try:
        cadastro = _lista_estacoes_ana()
    except Exception as exc:
        return [], [f"lista ANA: {type(exc).__name__}: {exc}"]

    if limite:
        cadastro = cadastro[:limite]

    def _uma(reg: dict) -> dict | None:
        codigo = (reg.get("CodEstacao") or "").strip()
        if not codigo:
            return None
        municipio_uf = (reg.get("Municipio-UF") or "").strip()
        est = {
            "id": f"ANA_{codigo}",
            "fonte": "ANA telemetria",
            "codigo": codigo,
            "nome": (reg.get("NomeEstacao") or codigo).strip().title(),
            "municipio": municipio_uf.rsplit("-", 1)[0].strip().title() or None,
            "rio": (reg.get("NomeRio") or "").strip().title() or None,
            "bacia": "Não catalogada (ANA)",
            "lat": _float(reg.get("Latitude")),
            "lon": _float(reg.get("Longitude")),
            "automatica": 1,
            "tipo": "PLUVIOMETRICA",
            # Sem cota oficial publicada por esta via -> sem classificação.
            "situacao": "Sem cota de referência publicada",
            "cor": "gray",
            "url_origem": (
                f"{ANA_BASE}DadosHidrometeorologicos?codEstacao={codigo}"
                "&dataInicio=&dataFim="
            ),
        }
        try:
            registros = _dados_ana(codigo)
        except Exception:
            registros = []
        if not registros:
            est["situacao"] = "Sem transmissão recente"
            return est

        registros.sort(key=lambda r: r.get("DataHora") or "")
        ultimo = registros[-1]
        est["medido_em"] = (ultimo.get("DataHora") or "").strip() or None
        est["nivel_cm"] = _float(ultimo.get("Nivel"))
        est["vazao_m3s"] = _float(ultimo.get("Vazao"))
        if est["nivel_cm"] is not None:
            est["tipo"] = "FLUVIOMETRICA"

        # Acumulados de chuva a partir dos registros horários reais.
        linhas = []
        for r in registros:
            chuva = _float(r.get("Chuva"))
            if chuva is None:
                continue
            linhas.append((r.get("DataHora"), chuva))
        if linhas:
            serie_chuva = pd.DataFrame(linhas, columns=["datahora", "valor"])
            if _chuva_plausivel(serie_chuva):
                est["_serie_chuva"] = serie_chuva
                est.update(_acumulados(serie_chuva))

        nivel_linhas = [
            (r.get("DataHora"), _float(r.get("Nivel")))
            for r in registros
            if _float(r.get("Nivel")) is not None
        ]
        if nivel_linhas:
            est["_serie_cota"] = pd.DataFrame(nivel_linhas, columns=["datahora", "valor"])
        return est

    with ThreadPoolExecutor(max_workers=trabalhadores) as ex:
        resultado = [e for e in ex.map(_uma, cadastro) if e]

    return resultado, erros


# -----------------------------------------------------------------------------
# DEDUPLICAÇÃO E GRAVAÇÃO
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# PADRÃO ÚNICO DE SAÍDA
# -----------------------------------------------------------------------------
# Toda estação, venha do SACE ou da ANA, passa por `_padronizar()` antes de ir
# para o banco. Depois disso, quem lê não precisa saber de onde veio: o
# contrato abaixo vale para todas as linhas.
#
#   id            TEXT   '<FONTE>_<chave da fonte>'            nunca nulo
#   fonte         TEXT   'SACE/SGB' | 'ANA telemetria'         nunca nulo
#   codigo        TEXT   código oficial na fonte               pode ser nulo
#   nome/municipio/rio  TEXT, sem espaço sobrando              nulo se ausente
#   bacia         TEXT   rótulo legível                        nunca nulo
#   lat/lon       REAL   graus decimais WGS84, validados no RS  nulo se inválido
#   tipo          TEXT   'FLUVIOMETRICA' | 'PLUVIOMETRICA'     nunca nulo
#   automatica    INT    1 = telemétrica/automática            nunca nulo
#   nivel_cm      REAL   CENTÍMETROS                           nulo se sem dado
#   cota_*_cm     REAL   CENTÍMETROS                           nulo se sem dado
#   vazao_m3s     REAL   m³/s                                  nulo se sem dado
#   chuva_*h      REAL   MILÍMETROS acumulados                 nulo se sem dado
#   medido_em     TEXT   'YYYY-MM-DD HH:MM:SS'                 nulo se sem dado
#   situacao      TEXT   vocabulário SITUACOES                 nunca nulo
#   cor           TEXT   green|gold|orange|red|purple|gray     nunca nulo
#
# A tabela `serie` segue o mesmo padrão: (id_estacao, grandeza, datahora,
# valor, unidade) com grandeza 'cota' (cm) ou 'chuva' (mm) e datahora no
# mesmo formato 'YYYY-MM-DD HH:MM:SS'.

SITUACOES = {
    "Normal": "green",
    "Cota de Atenção": "gold",
    "Cota de Alerta": "orange",
    "Cota de Inundação": "red",
    "Cota de Inundação Severa": "purple",
    "Seca Moderada": "purple",
    "Seca Excepcional": "red",
    "Sem transmissão": "gray",
    "Sem transmissão recente": "gray",
    "Sem cota de referência publicada": "gray",
}

UNIDADES = {"cota": "cm", "chuva": "mm"}

# Faixas físicas aceitáveis. Fora delas o valor NÃO é do tipo que a coluna
# promete, então vira NULL e o motivo fica registrado em `observacao`.
# Dois casos reais que motivaram isto:
#   - o SACE publicou "24.774 mm em 24 h" para Barra do Chapecó (o recorde de
#     24 h no RS é da ordem de 300 mm);
#   - usinas CGH/PCH na telemetria da ANA transmitem COTA ABSOLUTA em metros
#     (ex.: 88.130 = 881,30 m em Vacaria), não régua fluviométrica em cm.
FAIXAS = {
    "nivel_cm": (0.0, 5000.0),
    "cota_atencao_cm": (0.0, 5000.0),
    "cota_alerta_cm": (0.0, 5000.0),
    "cota_inundacao_cm": (0.0, 5000.0),
    "chuva_1h": (0.0, 150.0),
    "chuva_24h": (0.0, 500.0),
    "chuva_72h": (0.0, 1000.0),
    "vazao_m3s": (0.0, 200000.0),
}

# Caixa envolvente do Rio Grande do Sul (extremos reais do estado), usada para
# descartar coordenada corrompida e como desempate quando o gazetteer da ANA
# não tem estação próxima o bastante para cravar a UF.
CAIXA_RS = (-33.80, -27.05, -57.70, -49.65)  # lat_min, lat_max, lon_min, lon_max

_FORMATOS_DATA = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


def _data_padrao(valor) -> str | None:
    """Qualquer data das fontes -> 'YYYY-MM-DD HH:MM:SS'."""
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() in {"nan", "nat", "none"}:
        return None
    for formato in _FORMATOS_DATA:
        try:
            return datetime.strptime(texto, formato).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    convertida = pd.to_datetime(texto, errors="coerce", dayfirst=False)
    if pd.isna(convertida):
        return None
    return convertida.strftime("%Y-%m-%d %H:%M:%S")


def _texto_padrao(valor) -> str | None:
    if valor is None:
        return None
    texto = re.sub(r"\s+", " ", str(valor)).strip(" -\t")
    if not texto or texto.lower() in {"nan", "none", "null"}:
        return None
    return texto


def _padronizar(est: dict) -> dict:
    """Normaliza uma estação de QUALQUER fonte para o contrato único acima."""
    saida = dict(est)

    for campo in ("codigo", "nome", "municipio", "rio", "bacia", "fonte", "situacao"):
        saida[campo] = _texto_padrao(saida.get(campo))

    saida["nome"] = saida.get("nome") or saida.get("codigo") or saida["id"]
    saida["bacia"] = saida.get("bacia") or "Não catalogada"
    saida["fonte"] = saida.get("fonte") or "Desconhecida"

    lat, lon = _float(saida.get("lat")), _float(saida.get("lon"))
    if lat is None or lon is None or not (
        CAIXA_RS[0] <= lat <= CAIXA_RS[1] and CAIXA_RS[2] <= lon <= CAIXA_RS[3]
    ):
        lat = lon = None
    saida["lat"], saida["lon"] = lat, lon

    descartes = []
    for campo in (
        "nivel_cm", "vazao_m3s", "chuva_1h", "chuva_24h", "chuva_72h",
        "cota_atencao_cm", "cota_alerta_cm", "cota_inundacao_cm",
    ):
        valor = _float(saida.get(campo))
        minimo, maximo = FAIXAS[campo]
        if valor is not None and not (minimo <= valor <= maximo):
            descartes.append(f"{campo}={valor:g} fora da faixa [{minimo:g}, {maximo:g}]")
            valor = None
        saida[campo] = valor
    saida["observacao"] = "; ".join(descartes) or None

    saida["medido_em"] = _data_padrao(saida.get("medido_em"))
    if saida.get("nivel_cm") is not None or saida.get("tipo") == "FLUVIOMETRICA":
        saida["tipo"] = "FLUVIOMETRICA"
    else:
        saida["tipo"] = "PLUVIOMETRICA"
    saida["automatica"] = 1 if saida.get("automatica", 1) else 0

    situacao = saida.get("situacao") or "Sem cota de referência publicada"
    # Aceita a grafia variável do SGB ('Cota de inundação' / 'Cota de Inundação').
    for oficial in SITUACOES:
        if situacao.casefold() == oficial.casefold():
            situacao = oficial
            break
    saida["situacao"] = situacao
    saida["cor"] = SITUACOES.get(situacao, saida.get("cor") or "gray")

    for coluna in COLUNAS:
        saida.setdefault(coluna, None)
    return saida


def _muito_perto(a: dict, b: dict, graus: float = 0.002) -> bool:
    """~200 m. Usado só para não duplicar no mapa a mesma estação física que
    aparece no SACE (com cota oficial) e no cadastro da ANA (sem cota)."""
    if None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return False
    return abs(a["lat"] - b["lat"]) < graus and abs(a["lon"] - b["lon"]) < graus


def _classificar(est: dict) -> dict:
    """Aplica a cor pela cota REAL contra a cota OFICIAL. Sem limiar
    publicado, fica cinza — nunca arbitramos um valor."""
    nivel = est.get("nivel_cm")
    inund = est.get("cota_inundacao_cm")
    alerta = est.get("cota_alerta_cm")
    atencao = est.get("cota_atencao_cm")

    if nivel is None or inund is None or alerta is None or atencao is None:
        est.setdefault("situacao", "Sem cota de referência publicada")
        if est.get("cor") in (None, ""):
            est["cor"] = "gray"
        return est

    if nivel >= inund:
        est["situacao"], est["cor"] = "Cota de Inundação", "red"
    elif nivel >= alerta:
        est["situacao"], est["cor"] = "Cota de Alerta", "orange"
    elif nivel >= atencao:
        est["situacao"], est["cor"] = "Cota de Atenção", "gold"
    else:
        est["situacao"], est["cor"] = "Normal", "green"
    return est


COLUNAS = [
    "id", "fonte", "codigo", "nome", "municipio", "rio", "bacia", "lat", "lon",
    "tipo", "automatica", "cota_atencao_cm", "cota_alerta_cm", "cota_inundacao_cm",
    "nivel_cm", "vazao_m3s", "chuva_1h", "chuva_24h", "chuva_72h", "medido_em",
    "situacao", "cor", "observacao", "url_origem", "atualizado_em",
]


def _gravar(estacoes: list[dict]) -> None:
    """Grava tudo em uma transação só (as threads só buscam; quem escreve é
    a thread principal — evita 'database is locked')."""
    agora = _agora()
    linhas, series = [], []
    purgar: set[tuple[str, str]] = set()

    for est in estacoes:
        est["atualizado_em"] = agora
        linhas.append(tuple(est.get(c) for c in COLUNAS))
        if est.get("_purgar_chuva"):
            purgar.add((est["id"], "chuva"))
        for grandeza in ("cota", "chuva"):
            df = est.get(f"_serie_{grandeza}")
            if isinstance(df, pd.DataFrame) and not df.empty:
                for dh, valor in df.itertuples(index=False):
                    quando = _data_padrao(dh)
                    numero = _float(valor)
                    if quando and numero is not None:
                        series.append(
                            (est["id"], grandeza, quando, numero, UNIDADES[grandeza])
                        )
                # NÃO purga: a série ACUMULA.
                #
                # Antes cada coleta apagava a série inteira da estação e
                # regravava a janela nova. O banco virava um espelho dos ~30
                # dias que o SACE publica, e o que a fonte descartava sumia
                # para sempre — a cheia de julho de 2026, por exemplo, deixaria
                # de existir em meados de agosto.
                #
                # O purge era desnecessário: a chave primária é
                # (id_estacao, grandeza, datahora), então o INSERT OR REPLACE
                # já corrige valor revisado pela fonte no mesmo instante, sem
                # destruir o histórico anterior à janela.
                #
                # Purga continua acontecendo em UM caso, logo acima: série de
                # chuva rejeitada por implausibilidade, que precisa mesmo sair.

    with conectar() as con:
        if purgar:
            con.executemany(
                "DELETE FROM serie WHERE id_estacao=? AND grandeza=?", sorted(purgar)
            )
        con.executemany(
            f"INSERT OR REPLACE INTO estacao ({','.join(COLUNAS)}) "
            f"VALUES ({','.join('?' * len(COLUNAS))})",
            linhas,
        )
        if series:
            con.executemany(
                "INSERT OR REPLACE INTO serie "
                "(id_estacao, grandeza, datahora, valor, unidade) VALUES (?,?,?,?,?)",
                series,
            )


def sincronizar(
    incluir_ana: bool = True,
    limite_ana: int | None = None,
    baixar_series: bool = True,
    progresso=None,
) -> dict:
    """Roda a coleta real e atualiza o banco. `progresso` é um callable
    opcional `(fracao: float, mensagem: str)` para a barra do Streamlit."""
    criar_schema()
    iniciada = _agora()
    erros: list[str] = []

    def avisar(fracao, msg):
        if progresso:
            try:
                progresso(fracao, msg)
            except Exception:
                pass

    avisar(0.05, "Lendo mapas de bacias do SACE/SGB…")
    sace, erros_sace = coletar_sace(baixar_series=baixar_series)
    erros += erros_sace
    avisar(0.55, f"SACE: {len(sace)} estações com cota oficial.")

    ana: list[dict] = []
    if incluir_ana:
        avisar(0.60, "Consultando telemetria da ANA (estações automáticas do RS)…")
        ana, erros_ana = coletar_ana(limite=limite_ana)
        erros += erros_ana
        avisar(0.88, f"ANA: {len(ana)} estações telemétricas.")

    # SACE tem prioridade: é a única fonte com cota oficial de inundação.
    # `_padronizar` roda por último para que TODAS as linhas — não importa a
    # origem — saiam no mesmo formato, unidade e vocabulário.
    finais = [_padronizar(_classificar(e)) for e in sace]
    for cand in ana:
        if any(_muito_perto(cand, e) for e in finais):
            continue
        finais.append(_padronizar(_classificar(cand)))

    # Só Rio Grande do Sul. O SACE monitora a bacia do Uruguai inteira, então
    # vinham estações de Santa Catarina junto; aqui elas saem.
    dentro, rejeitadas = [], []
    for est in finais:
        (dentro if _dentro_do_rs(est) else rejeitadas).append(est)
    finais = dentro

    if rejeitadas:
        avisar(0.92, f"{len(rejeitadas)} estação(ões) fora do RS descartada(s).")
        # Apaga só as que ESTA coleta rejeitou — nunca as de outras fontes que
        # simplesmente não entraram nesta rodada.
        ids = sorted({e["id"] for e in rejeitadas})
        marcas = ",".join("?" * len(ids))
        with conectar() as con:
            con.execute(f"DELETE FROM estacao WHERE id IN ({marcas})", ids)
            con.execute(f"DELETE FROM serie WHERE id_estacao IN ({marcas})", ids)

    avisar(0.94, f"Gravando {len(finais)} estações no banco…")
    _gravar(finais)

    fontes = "SACE/SGB" + (" + ANA telemetria" if incluir_ana else "")
    with conectar() as con:
        con.execute(
            "INSERT INTO coleta (iniciada_em, terminada_em, fontes, estacoes, erros, detalhe) "
            "VALUES (?,?,?,?,?,?)",
            (iniciada, _agora(), fontes, len(finais), len(erros), " | ".join(erros[:20])),
        )

    avisar(1.0, "Concluído.")
    return {
        "estacoes": len(finais),
        "sace": len(sace),
        "ana": len(ana),
        "erros": erros,
        "iniciada_em": iniciada,
        "terminada_em": _agora(),
    }


# -----------------------------------------------------------------------------
# LEITURA (é só isso que a interface usa)
# -----------------------------------------------------------------------------
def carregar_estacoes() -> pd.DataFrame:
    criar_schema()
    with conectar() as con:
        df = pd.read_sql_query("SELECT * FROM estacao ORDER BY nome", con)
    if not df.empty:
        df["medido_em_dt"] = pd.to_datetime(df["medido_em"], errors="coerce")
    return df


def carregar_serie(id_estacao: str, grandeza: str, dias: int = 30) -> pd.DataFrame:
    with conectar() as con:
        df = pd.read_sql_query(
            "SELECT datahora, valor, unidade FROM serie "
            "WHERE id_estacao=? AND grandeza=? ORDER BY datahora",
            con,
            params=(id_estacao, grandeza),
        )
    if df.empty:
        return df
    df["datahora"] = pd.to_datetime(df["datahora"], errors="coerce")
    df = df.dropna(subset=["datahora"])
    corte = df["datahora"].max() - timedelta(days=dias)
    return df[df["datahora"] >= corte]


def ultima_coleta() -> dict | None:
    criar_schema()
    with conectar() as con:
        linha = con.execute(
            "SELECT iniciada_em, terminada_em, fontes, estacoes, erros "
            "FROM coleta ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not linha:
        return None
    return dict(
        zip(("iniciada_em", "terminada_em", "fontes", "estacoes", "erros"), linha)
    )


def idade_dados() -> timedelta | None:
    """Há quanto tempo foi a última coleta. None = nunca coletou."""
    ult = ultima_coleta()
    if not ult or not ult.get("terminada_em"):
        return None
    try:
        return datetime.now() - datetime.strptime(ult["terminada_em"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def nowcast_chuva(id_estacao: str, horas_base: int = 3) -> dict | None:
    """Extrapolação estatística da chuva recente — NÃO é previsão
    meteorológica. Usa a taxa média real das últimas `horas_base` horas.
    `confiabilidade` = fração das amostras esperadas que realmente existem.
    """
    serie = carregar_serie(id_estacao, "chuva", dias=2)
    if serie.empty:
        return None
    fim = serie["datahora"].max()
    janela = serie[serie["datahora"] > fim - timedelta(hours=horas_base)]
    if janela.empty:
        return None

    acumulado = float(janela["valor"].sum())
    taxa = acumulado / horas_base

    passos = janela["datahora"].diff().dt.total_seconds().dropna()
    passo_min = (passos.median() / 60) if not passos.empty else 15.0
    esperadas = max(1.0, horas_base * 60 / max(passo_min, 1.0))
    confiabilidade = min(1.0, len(janela) / esperadas)

    return {
        "taxa_recente_mm_h": round(taxa, 2),
        "acumulado_previsto_12h": round(taxa * 12, 1),
        "acumulado_previsto_24h": round(taxa * 24, 1),
        "confiabilidade": round(confiabilidade, 2),
        "base_horas": horas_base,
        "ate": fim,
    }


# -----------------------------------------------------------------------------
# PONTO DE EXTENSÃO — INMET
# -----------------------------------------------------------------------------
def coletar_inmet() -> tuple[list[dict], list[str]]:
    """Reservado. Hoje o INMET publica a LISTA de estações
    (`https://apitempo.inmet.gov.br/estacoes/T`, 78 operantes no RS) mas os
    endpoints de LEITURA respondem 204/404 sem token, e o portal está atrás
    de proteção anti-bot.

    Enquanto não houver token, esta função devolve vazio de propósito: uma
    estação sem leitura real não deve entrar no painel de decisão.
    Para ligar: obtenha o token do INMET e consuma
    `https://apitempo.inmet.gov.br/token/estacao/<ini>/<fim>/<cod>/<token>`,
    montando dicionários no mesmo formato de `coletar_ana()`.
    """
    return [], ["INMET: endpoints de leitura indisponíveis sem token (204/404)."]


def exportar_series_mensais(pasta: str | Path = "dados/serie") -> list[Path]:
    """Arquiva a série de 15 min em CSV mensal comprimido, versionado no Git.

    POR QUE ISTO EXISTE
    -------------------
    O SACE publica uma janela móvel de ~30 dias. O que sai dessa janela some da
    fonte — e, quando o banco apenas espelhava a janela, sumia do projeto
    também. Para um trabalho que vai analisar eventos meses depois, isso é
    perda de dado primário.

    POR QUE NO GIT E NÃO SÓ NO BANCO
    --------------------------------
    O banco fica só na máquina, e a coleta que roda com tudo fechado é a do
    GitHub Actions — cujo runner começa vazio a cada execução e é destruído no
    fim. Ele não tem banco para acumular. Gravando arquivo mensal versionado, a
    própria nuvem constrói o arquivo histórico, sem depender do seu computador
    estar ligado.

    COMO SE COMPORTA
    ----------------
    Um arquivo por mês (`2026-07.csv.gz`). A cada execução, o mês corrente é
    reescrito com a união do que já estava arquivado e do que veio agora —
    valores revisados pela fonte substituem os antigos, medições novas entram,
    e nada some. Meses fechados não são tocados.

    Tamanho: ~225 mil linhas/mês, que comprimidas ficam na casa de 1 a 2 MB.
    """
    destino = Path(pasta)
    destino.mkdir(parents=True, exist_ok=True)

    with conectar() as con:
        df = pd.read_sql_query(
            "SELECT id_estacao, grandeza, datahora, valor, unidade FROM serie "
            "ORDER BY id_estacao, grandeza, datahora",
            con,
        )
    if df.empty:
        return []

    df["_mes"] = df["datahora"].str.slice(0, 7)          # 'YYYY-MM'
    df = df[df["_mes"].str.match(r"^\d{4}-\d{2}$", na=False)]

    gerados: list[Path] = []
    chaves = ["id_estacao", "grandeza", "datahora"]

    for mes, bloco in df.groupby("_mes"):
        caminho = destino / f"{mes}.csv.gz"
        bloco = bloco.drop(columns=["_mes"])

        if caminho.exists():
            try:
                anterior = pd.read_csv(caminho, compression="gzip", dtype=str)
                anterior["valor"] = pd.to_numeric(anterior["valor"], errors="coerce")
                # `keep="last"` = o que veio agora prevalece sobre o arquivado,
                # para absorver revisão da fonte no mesmo instante de medição.
                bloco = (
                    pd.concat([anterior, bloco], ignore_index=True)
                    .drop_duplicates(subset=chaves, keep="last")
                    .sort_values(chaves)
                )
            except Exception:
                pass  # arquivo corrompido: reescreve com o que temos agora

        bloco.to_csv(caminho, index=False, compression="gzip")
        gerados.append(caminho)

    return gerados


def importar_series_arquivadas(pasta: str | Path = "dados/serie") -> int:
    """Recarrega os arquivos mensais para dentro do banco.

    É o caminho de volta: em máquina nova, `git clone` traz os arquivos e esta
    função reconstrói o histórico completo — inclusive o que o SACE já
    descartou e que uma coleta nova jamais recuperaria.
    """
    origem = Path(pasta)
    if not origem.exists():
        return 0

    criar_schema()
    total = 0
    for caminho in sorted(origem.glob("*.csv.gz")):
        try:
            df = pd.read_csv(caminho, compression="gzip")
        except Exception:
            continue
        linhas = [
            (r.id_estacao, r.grandeza, r.datahora, _float(r.valor), r.unidade)
            for r in df.itertuples()
            if _float(r.valor) is not None
        ]
        if not linhas:
            continue
        with conectar() as con:
            con.executemany(
                "INSERT OR REPLACE INTO serie "
                "(id_estacao, grandeza, datahora, valor, unidade) VALUES (?,?,?,?,?)",
                linhas,
            )
        total += len(linhas)
    return total


def exportar_snapshot(pasta: str | Path = "dados") -> list[Path]:
    """Publica o estado atual no FORMATO PADRÃO ÚNICO (CSV + JSON).

    É o que a automação do GitHub Actions versiona a cada rodada: o banco
    SQLite é grande e binário demais para o Git, mas o snapshot é pequeno,
    legível em diff e consumível por qualquer ferramenta.
    """
    destino = Path(pasta)
    destino.mkdir(parents=True, exist_ok=True)

    df = carregar_estacoes().drop(columns=["medido_em_dt"], errors="ignore")
    caminho_csv = destino / "estacoes.csv"
    caminho_json = destino / "estacoes.json"
    df.to_csv(caminho_csv, index=False, encoding="utf-8")
    df.to_json(caminho_json, orient="records", force_ascii=False, indent=2)

    ult = ultima_coleta() or {}
    caminho_meta = destino / "coleta.json"
    caminho_meta.write_text(
        json.dumps(
            {
                "gerado_em": _agora(),
                "ultima_coleta": ult,
                "total_estacoes": int(len(df)),
                "em_inundacao": int((df["cor"] == "red").sum()),
                "em_alerta": int((df["cor"] == "orange").sum()),
                "em_atencao": int((df["cor"] == "gold").sum()),
                "fontes": ["SACE/SGB-CPRM", "ANA/Telemetria"],
                "abrangencia": "Rio Grande do Sul",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return [caminho_csv, caminho_json, caminho_meta]


if __name__ == "__main__":  # execução direta: coleta e mostra um resumo
    import argparse

    ap = argparse.ArgumentParser(description="Coletor GeoRisk-RS (dados reais).")
    ap.add_argument("--sem-ana", action="store_true", help="coletar só o SACE (mais rápido)")
    ap.add_argument("--exportar", action="store_true", help="gerar dados/ em CSV+JSON")
    ap.add_argument("--importar-arquivo", action="store_true",
                    help="recarregar dados/serie/*.csv.gz para o banco (maquina nova)")
    args = ap.parse_args()

    if args.importar_arquivo:
        # Ação isolada: reconstruir o banco a partir do arquivo versionado,
        # sem coletar. É o passo de máquina nova, depois do `git clone`.
        n = importar_series_arquivadas()
        print(f"{n:,} registros históricos recarregados de dados/serie/")
        raise SystemExit(0)

    resumo = sincronizar(
        incluir_ana=not args.sem_ana,
        progresso=lambda f, m: print(f"[{f:5.0%}] {m}"),
    )
    print(json.dumps({k: v for k, v in resumo.items() if k != "erros"}, indent=2, default=str))
    if resumo["erros"]:
        print(f"\n{len(resumo['erros'])} avisos (primeiros 10):")
        for e in resumo["erros"][:10]:
            print("  -", e)

    if args.exportar:
        for caminho in exportar_snapshot():
            print("exportado:", caminho)
        for caminho in exportar_series_mensais():
            print("arquivado:", caminho)
