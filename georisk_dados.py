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

import io
import json
import re
import sqlite3
from contextlib import contextmanager
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
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
@contextmanager
def conectar():
    """Conexão que realmente FECHA ao sair do bloco.

    `with sqlite3.connect(...) as con` NÃO fecha a conexão — só faz commit ou
    rollback da transação. Como todo o projeto usa esse padrão, cada chamada
    deixava um descritor aberto: medido, 262 conexões vazadas numa única
    execução. Com o painel aberto por horas, atualizando a cada 15 min, isso
    chega ao limite de descritores do processo.

    Este gerenciador mantém o commit automático do comportamento anterior e
    acrescenta o `close()` que faltava.
    """
    con = sqlite3.connect(CAMINHO_BANCO, timeout=30, check_same_thread=False)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


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

            -- Gazetteer da ANA em cache. Ver `_cadastro_ana_nacional`: sem
            -- Curva-chave por estação, ajustada sobre pares medidos de
            -- cota x vazão do histórico da ANA. Ver `ajustar_curva_chave`.
            CREATE TABLE IF NOT EXISTS curva_chave (
                codigo         TEXT PRIMARY KEY,
                a              REAL,
                b              REAL,
                h0             REAL,
                r2             REAL,
                erro_mediano   REAL,
                n              INTEGER,
                h_min          REAL,
                h_max          REAL,
                aprovada       INTEGER,
                ajustada_em    TEXT
            );

            -- isto, o host da telemetria cair leva junto a coleta do SACE,
            -- que nada tem a ver com ele.
            CREATE TABLE IF NOT EXISTS gazetteer_ana (
                lat        REAL,
                lon        REAL,
                uf         TEXT,
                obtido_em  TEXT
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
# O SGB trocou a forma de desenhar as estações em agosto de 2026: saiu
# `L.marker([lat, lon], {icon: NomeDoStatus})` e entrou
# `L.circleMarker([lat, lon], {radius:…, fillColor: "#00FF33", …})`.
#
# A situação deixou de vir no NOME do ícone e passou a vir na COR em
# hexadecimal. O coletor ficou três dias devolvendo zero estação sem acusar
# erro — a página respondia normalmente, só não casava mais com o padrão.
#
# As duas formas ficam aceitas: se o SGB reverter, continua funcionando.
RE_MARCADOR = re.compile(
    r"relatorio\.php\?apenas_grafico=sim&bacia=(\w+)&pm=(\d*)&s=(\d*)&sr=(\d*)"
    r".*?L\.circleMarker\(\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]"
    r".*?fillColor:\s*\"(#[0-9A-Fa-f]{6})\""
    r".*?bindTooltip\(\s*\"([^\"]*)\"",
    re.S,
)

RE_MARCADOR_ANTIGO = re.compile(
    r"relatorio\.php\?apenas_grafico=sim&bacia=(\w+)&pm=(\d*)&s=(\d*)&sr=(\d*)"
    r".*?L\.marker\(\s*\[\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\]\s*,\s*\{icon:\s*(\w+)\}"
    r".*?bindTooltip\(\s*\"([^\"]*)\"",
    re.S,
)

# Cor do marcador -> situação. Conferido contra o texto "Situação:" que o
# `relatorio.php` publica para as mesmas estações.
COR_PARA_SITUACAO = {
    "#00ff33": ("Normal", "green"),
    "#00cc00": ("Normal", "green"),
    "#ffff00": ("Cota de Atenção", "gold"),
    "#ffff33": ("Cota de Atenção", "gold"),
    "#ff9900": ("Cota de Alerta", "orange"),
    "#ff9933": ("Cota de Alerta", "orange"),
    "#ff0033": ("Cota de Inundação", "red"),
    "#ff3333": ("Cota de Inundação", "red"),
    "#cc0000": ("Cota de Inundação Severa", "purple"),
    "#c4c4c4": ("Sem transmissão", "gray"),
    "#cccccc": ("Sem transmissão", "gray"),
    "#999999": ("Sem transmissão", "gray"),
}
RE_PONTOS_CHUVA = re.compile(r"const\s+pontosChuva\s*=\s*(\[.*?\]);", re.S)

RE_NIVEL = re.compile(r"<h2[^>]*>\s*([\d.,]+)\s*Cota\s*\(cm\)\s*</h2>", re.I)
RE_MEDICAO = re.compile(r"medi[çc][ãa]o:\s*<b>\s*([^<]+?)\s*</b>", re.I)
RE_SITUACAO = re.compile(r"Situa[çc][ãa]o:\s*<span[^>]*>\s*</span>\s*([^<]+?)\s*<br", re.I)
RE_RIO = re.compile(r"N[íi]vel do rio\s+(.+?)\s+e altura de chuva\s*-\s*(.+?)\s*<br", re.I)
# Fallback: em algumas estações a linha sai só como "... chuva - Nome<br".
RE_LOCAL = re.compile(r"chuva\s*-\s*([^<]+?)\s*<br", re.I)

# Valor-sentinela que o SACE usa para "sem medição" em alguns CSVs.
SENTINELA = 9999.0

# Fração do que a fonte já tem no banco abaixo da qual a remoção de órfãs é
# pulada. 0,80 tolera oscilação normal da rede e barra colheita parcial: a
# rodada que derrubou o banco de 552 para 285 trouxe 55 % da ANA.
PROPORCAO_MINIMA_PURGA = 0.80


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

    achados = [(*g[:6], g[6], g[7], "cor") for g in RE_MARCADOR.findall(html)]
    if not achados:
        # Formato antigo, com ícone nomeado.
        achados = [(*g[:6], g[6], g[7], "icone")
                   for g in RE_MARCADOR_ANTIGO.findall(html)]

    estacoes = []
    for bac, pm, s, sr, lat, lon, marca, tooltip, tipo_marca in achados:
        codigo, nome = _limpar_nome(tooltip)
        if tipo_marca == "cor":
            situacao, cor = COR_PARA_SITUACAO.get(
                marca.lower(), ("Sem classificação", "gray")
            )
        else:
            situacao, cor = ICONE_PARA_SITUACAO.get(
                marca, ("Sem classificação", "gray")
            )
        estacoes.append(
            {
                # CHAVE: bacia + pm, e nada mais.
                #
                # Antes era `SACE_{bacia}_{pm}_{s}_{sr}`. Os parâmetros `s` e
                # `sr` identificam séries do gráfico, não a estação, e o SGB os
                # renumerou: a mesma estação passou a chegar com id novo, o
                # antigo continuou no banco, e o resultado foram 55 coordenadas
                # com estações duplicadas — Porto Mauá gravado três vezes.
                #
                # O `pm` é estável: Iraí é pm=37 nas duas versões, Porto Mauá é
                # pm=56 nas três. A bacia entra porque a página 'guaiba'
                # reexibe estações de 'cai' e 'taquari', e usar a bacia real do
                # relatório deduplica isso sozinho.
                "id": f"SACE_{bac}_{pm}",
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


RE_ARRAY_JS = re.compile(r"const\s+(labels|valoresCota|valoresChuva)\s*=\s*\[(.*?)\]", re.S)


def _serie_da_pagina(html: str) -> dict[str, pd.DataFrame]:
    """Série de 15 min lida do próprio relatório, e não do CSV.

    POR QUE ESTA FUNÇÃO EXISTE
    --------------------------
    Em 07/08/2026 o SACE parou de atualizar `api/dados/<bacia>_<pm>_cota.csv`
    e não voltou: o arquivo continua respondendo 200, com o mesmo conteúdo de
    sempre, terminando naquela data. A página do relatório, porém, seguiu
    fresca — ela desenha o gráfico a partir de arrays embutidos no JS:

        const labels       = ['2026-07-12 21:15:00', …]
        const valoresCota  = [333.00, …]
        const valoresChuva = [0.00, …]

    São os mesmos 15 minutos, do mesmo órgão, atualizados. Ler daqui também
    ECONOMIZA REQUISIÇÕES: o relatório já é baixado para pegar nível e cotas,
    então as duas séries saem de graça, no lugar de dois CSV por estação.

    Estação pluviométrica traz só `labels` e `valoresChuva` — o que é correto,
    e o chamador decide o tipo pelo que veio.
    """
    achados = {m.group(1): m.group(2) for m in RE_ARRAY_JS.finditer(html)}
    rotulos = re.findall(r"'([^']+)'", achados.get("labels", ""))
    if not rotulos:
        return {}

    saida: dict[str, pd.DataFrame] = {}
    for chave, grandeza in (("valoresCota", "cota"), ("valoresChuva", "chuva")):
        if chave not in achados:
            continue
        valores = re.findall(r"-?\d+\.?\d*", achados[chave])
        if len(valores) != len(rotulos):
            # Desalinhamento é dado corrompido, não série curta: descartar é
            # mais seguro que casar pelo menor comprimento e deslocar tudo.
            continue
        df = pd.DataFrame({
            "datahora": pd.to_datetime(rotulos, errors="coerce"),
            "valor": pd.to_numeric(valores, errors="coerce"),
        }).dropna()
        df = df[df["valor"] != SENTINELA]
        if not df.empty:
            saida[grandeza] = df.sort_values("datahora").reset_index(drop=True)
    return saida


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

    # As séries saem da mesma página, sem requisição extra. Ver
    # `_serie_da_pagina` para por que não vêm mais do CSV.
    series = _serie_da_pagina(html)
    if series:
        dados["_series_da_pagina"] = series

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
                # A página é a fonte primária: o CSV do SACE congelou em
                # 07/08/2026 e segue respondendo 200 com dado velho, enquanto
                # o gráfico do relatório continua atualizado. O CSV fica de
                # reserva para o caso de a página mudar de formato.
                da_pagina = est.pop("_series_da_pagina", None) or {}
                cota = da_pagina.get("cota")
                chuva = da_pagina.get("chuva")
                if cota is None:
                    cota = _serie_sace(est["_bacia_url"], est["_pm"], "cota")
                if chuva is None:
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
    é ele que diz a UF de cada coordenada.

    NÃO LEVANTA quando o host está fora. Em 12/08/2026, às 23h21,
    `telemetriaws1.ana.gov.br` ficou inalcançável a partir do runner
    ("Network is unreachable") e a exceção subiu por `_dentro_do_rs` até
    derrubar a rodada inteira — inclusive a coleta do SACE, que estava no ar e
    nada tem a ver com esse host. Fonte indisponível é rotina; um filtro de UF
    não pode ser ponto único de falha para todo o resto.
    """
    global _cadastro_nacional
    if _cadastro_nacional is None:
        try:
            texto = _baixar(f"{ANA_BASE}ListaEstacoesTelemetricas?statusEstacoes=&origem=")
            _cadastro_nacional = _xml_registros(texto, "Table")
        except Exception:
            _cadastro_nacional = []
    return _cadastro_nacional


def _salvar_gazetteer(pontos: list[tuple[float, float, str]]) -> None:
    """Guarda o gazetteer no banco. É cadastro: muda pouco e vale reusar."""
    if not pontos:
        return
    try:
        with conectar() as con:
            con.execute("DELETE FROM gazetteer_ana")
            con.executemany(
                "INSERT INTO gazetteer_ana (lat, lon, uf, obtido_em) VALUES (?,?,?,?)",
                [(la, lo, uf, _agora()) for la, lo, uf in pontos],
            )
    except Exception:
        pass


def _gazetteer_salvo() -> list[tuple[float, float, str]]:
    try:
        with conectar() as con:
            return [
                (r[0], r[1], r[2])
                for r in con.execute("SELECT lat, lon, uf FROM gazetteer_ana")
            ]
    except Exception:
        return []


_gazetteer: list[tuple[float, float, str]] | None = None


def _gazetteer_uf() -> list[tuple[float, float, str]]:
    """(lat, lon, UF) de todas as estações do cadastro nacional.

    Três camadas, nesta ordem: o cadastro recém-baixado, o gazetteer guardado
    de rodadas anteriores, e — não havendo nenhum — lista vazia. A lista vazia
    não é falha: faz `_uf_da_coordenada` devolver None, e `_dentro_do_rs` cai
    na caixa envolvente do estado, que já existia como segundo critério.
    """
    global _gazetteer
    if _gazetteer is None:
        pontos = []
        for e in _cadastro_ana_nacional():
            lat, lon = _float(e.get("Latitude")), _float(e.get("Longitude"))
            uf = (e.get("Municipio-UF") or "").strip().upper()[-2:]
            if lat is not None and lon is not None and len(uf) == 2:
                pontos.append((lat, lon, uf))

        if pontos:
            _salvar_gazetteer(pontos)
        else:
            pontos = _gazetteer_salvo()
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

    # --- COLHEITA CURTA: avisa e REFAZ, antes de deixar a rodada seguir.
    #
    # Em 19/08/2026 a ANA devolveu 264 estações em vez das ~485 de sempre. A
    # rodada seguiu como se fosse normal e a purga de órfãs apagou 267 — o
    # banco caiu de 552 para 285. A guarda relativa impede o estrago, mas
    # deixava a rodada publicar um snapshot pela metade em silêncio.
    #
    # Agora a fonte curta é refeita uma vez, automaticamente. Falha de rede
    # costuma ser momentânea, e uma segunda tentativa custa dois minutos contra
    # três horas de dado degradado até a próxima rodada.
    colheita_curta: list[str] = []
    for nome_fonte, lista, recoletar in (
        ("SACE/SGB", sace, lambda: coletar_sace(baixar_series=baixar_series)),
        ("ANA telemetria", ana if incluir_ana else None,
         lambda: coletar_ana(limite=limite_ana)),
    ):
        if lista is None:
            continue
        with conectar() as con:
            esperado = con.execute(
                "SELECT COUNT(*) FROM estacao WHERE fonte=?", (nome_fonte,)
            ).fetchone()[0]
        if not esperado or len(lista) >= esperado * PROPORCAO_MINIMA_PURGA:
            continue

        avisar(0.90, f"{nome_fonte} trouxe {len(lista)} de ~{esperado}. Refazendo…")
        try:
            nova, erros_nova = recoletar()
        except Exception as exc:
            nova, erros_nova = [], [f"recoleta {nome_fonte}: {type(exc).__name__}"]
        erros += erros_nova

        if len(nova) > len(lista):
            avisar(0.92, f"{nome_fonte}: {len(lista)} → {len(nova)} na segunda tentativa.")
            if nome_fonte == "SACE/SGB":
                sace = nova
            else:
                ana = nova
            lista = nova

        if len(lista) < esperado * PROPORCAO_MINIMA_PURGA:
            aviso = (f"COLHEITA CURTA em {nome_fonte}: {len(lista)} estações "
                     f"contra {esperado} no banco "
                     f"({len(lista) / esperado:.0%}), mesmo após refazer")
            colheita_curta.append(aviso)
            erros.append(aviso)
            avisar(0.93, aviso)

    # --- Mesma estação publicada em duas bacias do SACE.
    #
    # A página do Guaíba reexibe estações do Taquari e do Caí, porque esses
    # rios drenam para o Guaíba. A mesma estação física chega então duas vezes,
    # com `pm` diferente em cada página — e as duas versões não são idênticas:
    # a coordenada difere uns 30 m e o código oficial vem em formatos distintos
    # (Santa Tereza como 8647260 e 86472600; Vacaria como 2850045 e 02850045).
    #
    # A cópia da página agregadora costuma vir SEM leitura. Ficamos com a que
    # tem dado; havendo empate, com a da bacia específica, que é a que carrega
    # as cotas oficiais.
    def _peso(est: dict) -> tuple:
        # A SÉRIE vem primeiro no desempate.
        #
        # Sem isso o critério errava: Muçum ficou com a cópia do Guaíba, que
        # não tem CSV de série, enquanto a do Taquari — com 2.801 pontos de
        # cota — foi descartada. A estação sumia do módulo hidrológico
        # inteiro, sem hidrograma nem projeção, embora o dado existisse.
        #
        # Nível e cota podem faltar por falha momentânea do relatório; a
        # presença da série é o sinal mais estável de qual cópia é a útil.
        serie = est.get("_serie_cota")
        tem_serie = serie is not None and len(serie) > 0
        tem_nivel = est.get("nivel_cm") is not None
        tem_cota = est.get("cota_inundacao_cm") is not None
        especifica = "guaiba" not in str(est.get("id", ""))
        return (tem_serie, tem_nivel, tem_cota, especifica)

    unicas: list[dict] = []
    for cand in sorted(sace, key=_peso, reverse=True):
        if any(_muito_perto(cand, ja, graus=0.002) for ja in unicas):
            continue
        unicas.append(cand)
    if len(unicas) < len(sace):
        avisar(0.56, f"{len(sace) - len(unicas)} duplicata(s) entre bacias removida(s).")
    sace = unicas

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

    # --- REMOVE ÓRFÃS: estação que sumiu da fonte tem de sumir do banco.
    #
    # `INSERT OR REPLACE` só insere e atualiza; nunca apaga. Estação que a
    # fonte parou de publicar — ou que mudou de id, como aconteceu quando o
    # SGB renumerou os parâmetros — ficava no banco para sempre, com a leitura
    # velha, aparecendo no mapa como se fosse atual. Eram 78 assim.
    #
    # A guarda importa: só limpamos a fonte que trouxe um número plausível de
    # estações nesta rodada. Sem isso, uma coleta que falhasse (como a do SACE
    # em agosto, que devolveu zero por três dias sem acusar erro) apagaria
    # todas as estações daquela fonte.
    #
    # O mínimo ABSOLUTO não bastava. Em 19/08/2026 a ANA devolveu 264 estações
    # em vez das ~485 de sempre — uma coleta parcial, não uma rede que encolheu.
    # Como 264 > 50, a guarda deixou passar e 267 estações foram apagadas: o
    # banco caiu de 552 para 285 numa única rodada.
    #
    # Agora a guarda é RELATIVA ao que a própria fonte vinha trazendo. Colheita
    # abaixo de PROPORCAO_MINIMA_PURGA do que existe no banco é tratada como
    # falha parcial, e nada é removido — órfã de verdade sobrevive mais uma
    # rodada, o que é infinitamente melhor que perder metade da rede.
    for fonte_nome, coletadas, minimo in (
        ("SACE/SGB", sace, 10),
        ("ANA telemetria", ana if incluir_ana else None, 50),
    ):
        if coletadas is None or len(coletadas) < minimo:
            continue
        vivos = {e["id"] for e in finais if e.get("fonte") == fonte_nome}
        if not vivos:
            continue
        with conectar() as con:
            no_banco = con.execute(
                "SELECT COUNT(*) FROM estacao WHERE fonte=?", (fonte_nome,)
            ).fetchone()[0]
        if no_banco and len(vivos) < no_banco * PROPORCAO_MINIMA_PURGA:
            erros.append(
                f"purga de {fonte_nome} pulada: {len(vivos)} estações contra "
                f"{no_banco} no banco — colheita parcial, não rede menor"
            )
            continue
        marcas = ",".join("?" * len(vivos))
        with conectar() as con:
            orfas = [
                r[0] for r in con.execute(
                    f"SELECT id FROM estacao WHERE fonte=? AND id NOT IN ({marcas})",
                    (fonte_nome, *sorted(vivos)),
                )
            ]
            if orfas:
                m2 = ",".join("?" * len(orfas))
                con.execute(f"DELETE FROM estacao WHERE id IN ({m2})", orfas)
                con.execute(f"DELETE FROM serie WHERE id_estacao IN ({m2})", orfas)
                avisar(0.96, f"{len(orfas)} estação(ões) órfã(s) de {fonte_nome} removida(s).")

    fontes = "SACE/SGB" + (" + ANA telemetria" if incluir_ana else "")
    with conectar() as con:
        con.execute(
            "INSERT INTO coleta (iniciada_em, terminada_em, fontes, estacoes, erros, detalhe) "
            "VALUES (?,?,?,?,?,?)",
            (iniciada, _agora(), fontes, len(finais), len(erros),
             " | ".join((colheita_curta + erros)[:20])),
        )

    avisar(1.0, "Concluído.")
    return {
        "estacoes": len(finais),
        "sace": len(sace),
        "ana": len(ana),
        "colheita_curta": colheita_curta,
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
            "SELECT iniciada_em, terminada_em, fontes, estacoes, erros, detalhe "
            "FROM coleta ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not linha:
        return None
    return dict(
        zip(("iniciada_em", "terminada_em", "fontes", "estacoes", "erros",
             "detalhe"), linha)
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


# -----------------------------------------------------------------------------
# SÉRIE HISTÓRICA DA ANA — anos de dado diário, para calibrar e validar
# -----------------------------------------------------------------------------
# A telemetria só devolve os últimos dias e o SACE publica uma janela de 30. Com
# isso o projeto tinha UM evento de cheia para treinar, e qualquer calibração
# viraria decorar aquele caso.
#
# `HidroSerieHistorica` do mesmo serviço da ANA resolve: devolve a série DIÁRIA
# consistida, com mais de 15 anos de cobertura. Verificado em Encantado —
# 2010 a 2025 sem lacuna, e a cheia catastrófica de maio de 2024 aparece com
# 2.314 cm em 02/05/2024, contra cota de inundação de 1.200 cm.
#
# É dado diário, não de 15 min. Então NÃO serve para o tempo de resposta (Tc),
# que precisa de resolução sub-diária. Serve para o que depende de evento:
# calibrar os limiares de encharcamento, aferir o CN, e montar um catálogo de
# cheias históricas para validar o método fora do período coletado.
#
# FORMATO DA FONTE: cada registro é um MÊS, com os dias em colunas separadas
# (`Cota01`..`Cota31`, `Chuva01`..`Chuva31`). É preciso desdobrar.
#
# CONSISTÊNCIA: a ANA devolve o mesmo dia duas vezes quando há versão bruta
# (NivelConsistencia=1) e consistida (=2). Ficamos com a consistida, que passou
# por crítica técnica.

URL_SERIE_HISTORICA = ANA_BASE + "HidroSerieHistorica"

TIPO_HISTORICO = {"cota": (1, "Cota"), "chuva": (2, "Chuva"), "vazao": (3, "Vazao")}


def serie_historica_ana(
    codigo_estacao: str,
    data_inicio: str,
    data_fim: str,
    grandeza: str = "cota",
) -> pd.DataFrame:
    """Série DIÁRIA histórica de uma estação da ANA.

    Datas em `dd/mm/aaaa`. Devolve colunas `datahora`, `valor`, `consistencia`.
    Quando o mesmo dia vem em versão bruta e consistida, mantém a consistida.
    """
    if grandeza not in TIPO_HISTORICO:
        raise ValueError(f"grandeza deve ser uma de {list(TIPO_HISTORICO)}")
    tipo, prefixo = TIPO_HISTORICO[grandeza]

    resposta = _sessao().get(
        URL_SERIE_HISTORICA,
        params={
            "codEstacao": str(codigo_estacao),
            "dataInicio": data_inicio,
            "dataFim": data_fim,
            "tipoDados": str(tipo),
            "nivelConsistencia": "",
        },
        timeout=300,
    )
    resposta.raise_for_status()

    try:
        raiz = ET.fromstring(resposta.text)
    except ET.ParseError:
        return pd.DataFrame(columns=["datahora", "valor", "consistencia"])

    # ESTRUTURA DA FONTE, decifrada na marra: para cada mês vêm ATÉ TRÊS
    # registros, distinguidos pela hora do `DataHora` e pelo campo
    # `MediaDiaria`:
    #
    #     00:00  MediaDiaria=1  -> série da média diária
    #     07:00  MediaDiaria=0  -> leitura das 7 h
    #     17:00  MediaDiaria=0  -> leitura das 17 h
    #
    # Empilhar as três como se fossem a mesma série produz três valores por dia
    # com significados diferentes — foi o erro da primeira versão.
    #
    # E a escolha importa: no pico da cheia de maio/2024 em Encantado, os
    # 2.314 cm de 02/05 aparecem SÓ na leitura das 17 h; a série de média
    # diária nem tem valor para aquele dia. Usar apenas a média perderia o
    # pico, que é justamente o que interessa em análise de cheia. Por isso
    # devolvemos as três colunas e o máximo do dia.
    registros: dict[str, dict] = {}
    for registro in raiz.iter():
        if registro.tag.split("}")[-1] != "SerieHistorica":
            continue
        campos = {c.tag.split("}")[-1]: c.text for c in registro}
        base = pd.to_datetime(campos.get("DataHora"), errors="coerce")
        if pd.isna(base):
            continue

        media_diaria = (campos.get("MediaDiaria") or "0").strip() == "1"
        if media_diaria:
            serie_nome = "media_diaria"
        elif base.hour == 7:
            serie_nome = "leitura_07h"
        elif base.hour == 17:
            serie_nome = "leitura_17h"
        else:
            serie_nome = f"leitura_{base.hour:02d}h"

        consistencia = int(_float(campos.get("NivelConsistencia")) or 1)

        for dia in range(1, 32):
            valor = _float(campos.get(f"{prefixo}{dia:02d}"))
            if valor is None:
                continue
            try:
                quando = base.normalize() + timedelta(days=dia - 1)
            except (ValueError, OverflowError):
                continue
            if quando.month != base.month:
                continue  # mês curto: 30 de fevereiro não existe

            chave = quando.strftime("%Y-%m-%d")
            linha = registros.setdefault(
                chave, {"datahora": quando, "consistencia": consistencia}
            )
            # Consistida (2) prevalece sobre bruta (1) para a mesma coluna.
            if serie_nome not in linha or consistencia >= linha["consistencia"]:
                linha[serie_nome] = valor
                linha["consistencia"] = max(linha["consistencia"], consistencia)

    if not registros:
        return pd.DataFrame(
            columns=["datahora", "media_diaria", "leitura_07h", "leitura_17h",
                     "valor", "consistencia"]
        )

    df = pd.DataFrame(list(registros.values())).sort_values("datahora")

    # `valor` = máximo do dia entre TODAS as séries que a fonte trouxe.
    #
    # Não dá para fixar as colunas em ("media_diaria", "leitura_07h",
    # "leitura_17h"): isso vale para cota, mas a chuva vem com outro padrão de
    # hora e caía fora da conta, produzindo NaN em toda a série. Somar o que
    # existir cobre os dois casos e qualquer variação futura da fonte.
    colunas_serie = [
        c for c in df.columns if c not in ("datahora", "consistencia")
    ]
    df["valor"] = df[colunas_serie].max(axis=1) if colunas_serie else np.nan

    for coluna in ("media_diaria", "leitura_07h", "leitura_17h"):
        if coluna not in df.columns:
            df[coluna] = np.nan

    return df[
        ["datahora", "valor", "media_diaria", "leitura_07h", "leitura_17h",
         "consistencia"]
    ].reset_index(drop=True)


def criar_schema_historico() -> None:
    with conectar() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS serie_historica (
                codigo_estacao TEXT,
                grandeza       TEXT,          -- 'cota' | 'chuva' | 'vazao'
                datahora       TEXT,          -- 'YYYY-MM-DD' (diário)
                valor          REAL,          -- máximo do dia entre as séries
                consistencia   INTEGER,       -- 1 bruto, 2 consistido
                media_diaria   REAL,          -- as três séries que a ANA
                leitura_07h    REAL,          -- devolve separadas, guardadas
                leitura_17h    REAL,          -- para quem precisar de cada uma
                PRIMARY KEY (codigo_estacao, grandeza, datahora)
            );
            CREATE INDEX IF NOT EXISTS ix_hist ON serie_historica
                (codigo_estacao, grandeza, datahora);
            """
        )
        colunas = {l[1] for l in con.execute("PRAGMA table_info(serie_historica)")}
        for nova in ("media_diaria", "leitura_07h", "leitura_17h"):
            if nova not in colunas:
                con.execute(f"ALTER TABLE serie_historica ADD COLUMN {nova} REAL")


def coletar_historico(
    anos_atras: int = 15,
    grandezas: tuple[str, ...] = ("cota", "chuva"),
    limite_estacoes: int | None = None,
    progresso=None,
) -> dict:
    """Baixa a série histórica diária das estações do RS já cadastradas.

    Roda uma vez e fica no banco: é dado consolidado, não muda. Serve de base
    para calibração e para validar o método em eventos fora do período que a
    coleta corrente alcança.
    """
    criar_schema_historico()
    fim = datetime.now()
    inicio = fim - timedelta(days=365 * anos_atras)
    fmt = "%d/%m/%Y"

    # QUAIS ESTAÇÕES: as do SACE, não as da telemetria.
    #
    # Medido: nenhuma das ~500 estações telemétricas do banco tem série
    # histórica — são pontos de monitoramento de usina (UHE/PCH/CGH), obras
    # recentes sem registro longo. Quem tem década de dado são as
    # fluviométricas tradicionais da Rede Hidrometeorológica Nacional, que são
    # justamente as que o SACE acompanha e para as quais existe cota oficial.
    #
    # O código do SACE é o da ANA truncado: 8672000 (SACE) -> 86720000 (ANA).
    # Confirmado em 9 de 12 estações testadas. Os códigos de 6 dígitos exigem
    # preenchimento diferente, então tentamos as variantes.
    with conectar() as con:
        estacoes = con.execute(
            "SELECT DISTINCT codigo, nome FROM estacao "
            "WHERE fonte = 'SACE/SGB' AND codigo IS NOT NULL AND codigo != '' "
            "ORDER BY nome"
        ).fetchall()

    # O `limite_estacoes` tinha parado de ser aplicado quando a consulta foi
    # reescrita para apontar às estações do SACE: quem pedisse 6 recebia todas.
    if limite_estacoes:
        estacoes = estacoes[:limite_estacoes]

    def variantes(codigo: str) -> list[str]:
        """Formatos possíveis do código na base histórica da ANA."""
        base = str(codigo).strip()
        candidatos = [base + "0", base.ljust(8, "0"), base.zfill(8), base]
        vistos, saida = set(), []
        for c in candidatos:
            if c not in vistos and len(c) <= 8:
                vistos.add(c)
                saida.append(c)
        return saida

    total, erros = 0, []
    for i, (codigo, nome) in enumerate(estacoes):
        if progresso:
            try:
                progresso(i / max(len(estacoes), 1), f"{nome} ({codigo})…")
            except Exception:
                pass
        for grandeza in grandezas:
            df, codigo_ana = None, None
            for tentativa in variantes(codigo):
                try:
                    candidato = serie_historica_ana(
                        tentativa, inicio.strftime(fmt), fim.strftime(fmt), grandeza
                    )
                except Exception as exc:
                    erros.append(f"{tentativa}/{grandeza}: {type(exc).__name__}")
                    continue
                if not candidato.empty:
                    df, codigo_ana = candidato, tentativa
                    break
            if df is None or df.empty:
                continue
            # Acesso por NOME, não por posição: `serie_historica_ana` devolve as
            # três séries da fonte em colunas separadas (média diária, 07 h e
            # 17 h) além de `valor` e `consistencia`, e desempacotar por
            # posição quebra a cada coluna nova.
            linhas = [
                (
                    codigo_ana, grandeza,
                    linha.datahora.strftime("%Y-%m-%d"),
                    float(linha.valor),
                    int(linha.consistencia),
                    None if pd.isna(linha.media_diaria) else float(linha.media_diaria),
                    None if pd.isna(linha.leitura_07h) else float(linha.leitura_07h),
                    None if pd.isna(linha.leitura_17h) else float(linha.leitura_17h),
                )
                for linha in df.itertuples(index=False)
                if pd.notna(linha.valor)
            ]
            with conectar() as con:
                con.executemany(
                    "INSERT OR REPLACE INTO serie_historica "
                    "(codigo_estacao, grandeza, datahora, valor, consistencia, "
                    " media_diaria, leitura_07h, leitura_17h) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    linhas,
                )
            total += len(linhas)

    return {"registros": total, "estacoes": len(estacoes), "erros": erros[:20]}


def carregar_historico(
    codigo_estacao: str, grandeza: str = "cota"
) -> pd.DataFrame:
    criar_schema_historico()
    with conectar() as con:
        df = pd.read_sql_query(
            "SELECT datahora, valor FROM serie_historica "
            "WHERE codigo_estacao=? AND grandeza=? ORDER BY datahora",
            con, params=(codigo_estacao, grandeza),
        )
    if not df.empty:
        df["datahora"] = pd.to_datetime(df["datahora"], errors="coerce")
    return df


def catalogo_de_cheias(
    codigo_estacao: str, cota_inundacao_cm: float, folga_dias: int = 5
) -> pd.DataFrame:
    """Eventos históricos em que a estação passou da cota de inundação.

    É a matéria-prima para validar o método fora do período coletado: cada
    linha é uma cheia real, com data do pico e nível atingido.
    """
    serie = carregar_historico(codigo_estacao, "cota")
    if serie.empty:
        return serie

    acima = serie[serie["valor"] >= cota_inundacao_cm].copy()
    if acima.empty:
        return acima

    # Agrupa dias consecutivos num único evento.
    acima["_gap"] = acima["datahora"].diff().dt.days.fillna(999)
    acima["_evento"] = (acima["_gap"] > folga_dias).cumsum()

    return (
        acima.groupby("_evento")
        .agg(
            inicio=("datahora", "min"),
            fim=("datahora", "max"),
            pico_cm=("valor", "max"),
            dias=("valor", "size"),
        )
        .reset_index(drop=True)
        .sort_values("pico_cm", ascending=False)
    )


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


# -----------------------------------------------------------------------------
# CHUVA HISTÓRICA DO INMET
# -----------------------------------------------------------------------------
# O INMET estava marcado como fora, e a marcação estava certa pelo motivo
# errado: a API de leitura (`apitempo.inmet.gov.br/estacao/...`) devolve 204
# vazio para toda estação e toda data, com ou sem token. Testado de novo em
# 19/08/2026 nas 93 automáticas do RS: 204 em todas.
#
# Mas o PORTAL publica o arquivo histórico completo, aberto e sem token:
#
#     https://portal.inmet.gov.br/uploads/dadoshistoricos/<ano>.zip
#
#     2023 → 107,1 MB     2025 →  90,9 MB
#     2024 → 102,8 MB     2026 →  55,1 MB
#
# Cada zip traz um CSV por estação, com PRECIPITAÇÃO TOTAL HORÁRIA em mm e as
# coordenadas no cabeçalho. Só de 2026 são 98 estações do RS.
#
# POR QUE ISSO IMPORTA MAIS QUE PARECE
# O confronto com cheias reais mostrou que nível e tendência são cegos ANTES da
# subida começar, e que a chuva é o único termo capaz de ver antes. O teste
# histórico não pôde medir isso porque só 2 estações tinham chuva no arquivo.
# Com o INMET passam a ser ~98, de hora em hora, cobrindo setembro de 2023 e
# maio de 2024 — as duas cheias que interessa reproduzir.

INMET_HISTORICO = "https://portal.inmet.gov.br/uploads/dadoshistoricos"

# Coluna da chuva no CSV do INMET. O cabeçalho tem acento e vírgula, então o
# casamento é por prefixo em vez de igualdade.
PREFIXO_COLUNA_CHUVA = "PRECIPITA"


def _parsear_csv_inmet(bruto: str) -> tuple[dict, pd.DataFrame]:
    """Cabeçalho (nome, código, lat, lon) e série horária de chuva de um CSV."""
    linhas = bruto.splitlines()
    cabecalho: dict = {}
    for linha in linhas[:8]:
        if ";" not in linha:
            continue
        chave, valor = linha.split(";", 1)
        cabecalho[chave.strip().rstrip(":").upper()] = valor.strip()

    inicio = next(
        (i for i, l in enumerate(linhas) if l.upper().startswith("DATA;")), None
    )
    if inicio is None:
        return cabecalho, pd.DataFrame()

    colunas = [c.strip() for c in linhas[inicio].split(";")]
    idx_chuva = next(
        (i for i, c in enumerate(colunas) if c.upper().startswith(PREFIXO_COLUNA_CHUVA)),
        None,
    )
    if idx_chuva is None:
        return cabecalho, pd.DataFrame()

    registros = []
    for linha in linhas[inicio + 1:]:
        partes = linha.split(";")
        if len(partes) <= idx_chuva:
            continue
        # O INMET usa vírgula decimal e deixa o campo vazio quando não mediu.
        valor = partes[idx_chuva].strip().replace(",", ".")
        if not valor:
            continue
        try:
            mm = float(valor)
        except ValueError:
            continue
        if mm < 0 or mm > 300:      # 300 mm em UMA hora não existe no RS
            continue
        hora = partes[1].strip().replace(" UTC", "").zfill(4)
        registros.append((f"{partes[0].strip()} {hora[:2]}:{hora[2:]}", mm))

    if not registros:
        return cabecalho, pd.DataFrame()
    df = pd.DataFrame(registros, columns=["datahora", "valor"])
    df["datahora"] = pd.to_datetime(df["datahora"], format="%Y/%m/%d %H:%M",
                                    errors="coerce")
    return cabecalho, df.dropna()


def coletar_historico_inmet(
    anos: tuple[int, ...] = (2024,), uf: str = "RS", progresso=None
) -> dict:
    """Baixa o arquivo histórico do INMET e grava a chuva horária do RS."""
    import zipfile

    criar_schema()
    criar_schema_historico()

    def avisar(f, m):
        if progresso:
            try:
                progresso(f, m)
            except Exception:
                pass

    total, estacoes, erros = 0, set(), []
    for k, ano in enumerate(anos):
        avisar(k / len(anos), f"Baixando {ano} do INMET…")
        try:
            resposta = _sessao().get(
                f"{INMET_HISTORICO}/{ano}.zip", timeout=600, stream=True
            )
            conteudo = io.BytesIO(resposta.content)
            arquivo = zipfile.ZipFile(conteudo)
        except Exception as erro:
            erros.append(f"{ano}: {type(erro).__name__}")
            continue

        alvos = [n for n in arquivo.namelist() if f"_{uf}_" in n.upper()]
        avisar(k / len(anos), f"{ano}: {len(alvos)} estações de {uf}")

        for n, nome in enumerate(alvos):
            try:
                cabecalho, serie = _parsear_csv_inmet(
                    arquivo.read(nome).decode("latin-1")
                )
            except Exception:
                continue
            if serie.empty:
                continue

            codigo = f"INMET_{cabecalho.get('CODIGO (WMO)', '').strip()}"
            estacoes.add(codigo)
            with conectar() as con:
                con.executemany(
                    "INSERT OR REPLACE INTO serie_historica "
                    "(codigo_estacao, grandeza, datahora, valor, consistencia) "
                    "VALUES (?,?,?,?,?)",
                    [(codigo, "chuva", str(d), float(v), 1)
                     for d, v in serie.itertuples(index=False)],
                )
            total += len(serie)
            if n % 20 == 0:
                avisar((k + n / max(len(alvos), 1)) / len(anos),
                       f"{ano}: {n}/{len(alvos)} estações")

    return {"registros": total, "estacoes": len(estacoes),
            "anos": list(anos), "erros": erros}


# -----------------------------------------------------------------------------
# CURVA-CHAVE — vazão a partir do nível
# -----------------------------------------------------------------------------
# O SACE publica nível e NÃO publica vazão: zero das 59 estações dele têm o
# campo preenchido, e o boletim mostrava "Vazão: Sem dado". A ANA publica vazão
# para parte da rede, e — mais útil — publica SÉRIE HISTÓRICA de vazão junto com
# a de cota, o que dá milhares de pares medidos na mesma estação.
#
# Com esses pares se ajusta a curva-chave clássica:
#
#     Q = a · (h − h₀)^b
#
# Não é invenção: são medições da própria estação, e o ajuste é verificável.
# Medido sobre 2015–2025:
#
#     Uruguaiana  r² 0,998   erro mediano  1,4 %
#     Estrela     r² 0,988                 1,7 %
#     Muçum       r² 0,973                13,3 %
#     Iraí        r² 0,966                10,9 %
#     Encantado   r² 0,493                49,9 %   <- reprovada
#
# Encantado mostra por que existe corte: metade das curvas serve, e aceitar
# todas seria trocar "sem dado" por número errado, que é pior.

R2_MINIMO_CURVA = 0.95
ERRO_MAXIMO_CURVA_PCT = 20.0

# Quanto se aceita extrapolar acima do maior nível já medido com vazão.
# Curva-chave extrapola mal, e cheia é justamente a região de extrapolação —
# então acima disso a vazão volta a ser "sem dado" em vez de virar chute.
MARGEM_EXTRAPOLACAO = 1.15


def ajustar_curva_chave(
    codigo: str, inicio: str = "01/01/2015", fim: str = "31/12/2025"
) -> dict | None:
    """Ajusta e VALIDA a curva-chave de uma estação. None se não há par."""
    try:
        vazao = serie_historica_ana(codigo, inicio, fim, "vazao")[["datahora", "valor"]]
        cota = serie_historica_ana(codigo, inicio, fim, "cota")[["datahora", "valor"]]
    except Exception:
        return None
    if vazao.empty or cota.empty:
        return None

    par = cota.merge(vazao, on="datahora", suffixes=("_h", "_q")).dropna()
    par = par[(par["valor_h"] > 0) & (par["valor_q"] > 0)]
    if len(par) < 200:
        return None

    h, q = par["valor_h"].to_numpy(float), par["valor_q"].to_numpy(float)
    # h₀ é o nível de vazão nula, que não é o zero da régua. Varrido em vez de
    # otimizado: a busca é barata e evita depender de scipy, ausente aqui.
    melhor = None
    for h0 in np.arange(0.0, h.min(), max(h.min() / 50.0, 0.5)):
        x, y = np.log(h - h0), np.log(q)
        b, log_a = np.polyfit(x, y, 1)
        r2 = float(np.corrcoef(x, y)[0, 1] ** 2)
        if melhor is None or r2 > melhor[0]:
            melhor = (r2, float(h0), float(b), float(np.exp(log_a)))
    if melhor is None:
        return None

    r2, h0, b, a = melhor
    previsto = a * np.power(h - h0, b)
    erro = float(np.median(np.abs(previsto - q) / q * 100.0))
    return {
        "codigo": str(codigo), "a": a, "b": b, "h0": h0, "r2": round(r2, 4),
        "erro_mediano": round(erro, 1), "n": int(len(par)),
        "h_min": float(h.min()), "h_max": float(h.max()),
        "aprovada": int(r2 >= R2_MINIMO_CURVA and erro <= ERRO_MAXIMO_CURVA_PCT),
        "ajustada_em": _agora(),
    }


def atualizar_curvas_chave(limite: int | None = None) -> pd.DataFrame:
    """Ajusta a curva-chave das estações fluviométricas e grava."""
    criar_schema()
    with conectar() as con:
        alvos = pd.read_sql_query(
            "SELECT id, nome, codigo FROM estacao "
            "WHERE tipo='FLUVIOMETRICA' AND codigo IS NOT NULL", con
        )

    linhas = []
    for i, r in enumerate(alvos.itertuples()):
        if limite and i >= limite:
            break
        # Mesmo zero final que já atrapalhou a área de drenagem e o histórico:
        # São Leopoldo é 8738200 no cadastro e 87380000 na ANA.
        for candidato in (str(r.codigo).strip(), str(r.codigo).strip() + "0"):
            ajuste = ajustar_curva_chave(candidato)
            if ajuste:
                linhas.append(ajuste)
                break

    if linhas:
        with conectar() as con:
            con.executemany(
                "INSERT OR REPLACE INTO curva_chave "
                "(codigo,a,b,h0,r2,erro_mediano,n,h_min,h_max,aprovada,ajustada_em) "
                "VALUES (:codigo,:a,:b,:h0,:r2,:erro_mediano,:n,:h_min,:h_max,"
                ":aprovada,:ajustada_em)",
                linhas,
            )
    return pd.DataFrame(linhas)


def vazao_estimada(codigo: str, nivel_cm: float) -> dict | None:
    """Vazão pela curva-chave da estação. None quando não dá para afirmar.

    Devolve None em três casos, e cada um é deliberado: sem curva ajustada,
    curva reprovada na validação, ou nível acima da faixa em que a curva foi
    medida. O terceiro é o mais importante — extrapolar curva-chave em cheia é
    exatamente onde ela erra mais.
    """
    if nivel_cm is None or pd.isna(nivel_cm):
        return None
    try:
        with conectar() as con:
            linha = con.execute(
                "SELECT a,b,h0,r2,erro_mediano,h_min,h_max,aprovada FROM curva_chave "
                "WHERE codigo IN (?,?)",
                (str(codigo).strip(), str(codigo).strip() + "0"),
            ).fetchone()
    except Exception:
        return None
    if not linha:
        return None

    a, b, h0, r2, erro, h_min, h_max, aprovada = linha
    if not aprovada:
        return None
    if nivel_cm <= h0 or nivel_cm > h_max * MARGEM_EXTRAPOLACAO:
        return None

    return {
        "vazao_m3s": round(float(a * (float(nivel_cm) - h0) ** b), 1),
        "erro_tipico_pct": erro, "r2": r2,
        "faixa_medida_cm": (h_min, h_max),
        "extrapolando": bool(nivel_cm > h_max),
        "origem": "curva-chave da própria estação",
    }


# -----------------------------------------------------------------------------
# VISÃO UNIFICADA — cada campo pela fonte que o serve melhor
# -----------------------------------------------------------------------------
# Nem o SACE nem a ANA bastam sozinhos, e a cobertura de cada um é bem desigual
# conforme o campo. Medido nas 552 estações:
#
#     campo                 ANA      SACE     quem serve melhor
#     cota_atencao/inund.     0 %     67 %     só o SACE tem
#     nivel_cm                5 %     87 %     SACE
#     chuva_24h               6 %     93 %     SACE
#     vazao_m3s               6 %      0 %     só a ANA tem
#     rio                    88 %     63 %     ANA
#
# Esta visão NÃO migra nada. O campo `bacia` continua com o rótulo de origem,
# porque `cn_da_bacia` e a caracterização geológica são indexadas por ele — 67
# estações perderiam o CN real e cairiam no genérico 75, junto com a densidade
# de drenagem e a de lineamentos, que são 3 das 11 variáveis do modelo.
#
# Em vez disso ela ACRESCENTA uma leitura consolidada, com a procedência de
# cada campo declarada ao lado. Quem quiser o dado cru continua lendo `estacao`;
# quem quiser o melhor disponível lê daqui e sabe de onde veio cada número.

CAMPOS_UNIFICADOS = (
    "id", "nome", "municipio", "fonte", "tipo", "lat", "lon",
    "rio", "bacia", "bacia_oficial", "area_drenagem_km2",
    "nivel_cm", "medido_em", "chuva_24h", "chuva_72h",
    "vazao_m3s", "origem_vazao",
    "cota_atencao_cm", "cota_alerta_cm", "cota_inundacao_cm",
    "situacao", "cor", "observacao",
)


def visao_unificada() -> pd.DataFrame:
    """Estado de cada estação com o melhor valor disponível por campo.

    Acrescenta três coisas que o cadastro cru não tem, cada uma vinda de onde
    ela existe de verdade:

    * `bacia_oficial` — as 26 bacias do shapefile do RS, preenchida para 550
      das 552. O campo `bacia` do cadastro tem 4 rótulos e joga as 485 estações
      da ANA em "Não catalogada", o que as torna invisíveis a qualquer consulta
      por bacia.
    * `area_drenagem_km2` — a área que a estação drena, do inventário da ANA.
    * `vazao_m3s` completada pela curva-chave quando a fonte não mede, com
      `origem_vazao` dizendo se o número foi medido ou estimado.

    Não substitui `estacao`: é leitura derivada, reconstruível a qualquer hora.
    """
    criar_schema()
    with conectar() as con:
        df = pd.read_sql_query(
            """
            SELECT e.*,
                   b.bacia    AS bacia_oficial,
                   a.area_km2 AS area_drenagem_km2
            FROM estacao e
            LEFT JOIN bacia_oficial b ON b.id_estacao = e.id
            LEFT JOIN area_drenagem a ON a.id_estacao = e.id
            """,
            con,
        )

    # --- Vazão: medida onde existe, estimada pela curva-chave onde não existe.
    # A estimativa já se recusa a extrapolar acima da faixa medida, então onde
    # ela devolve nada o campo continua nulo em vez de virar chute.
    origens, vazoes = [], []
    for r in df.itertuples():
        medida = getattr(r, "vazao_m3s", None)
        if medida is not None and not pd.isna(medida):
            vazoes.append(float(medida))
            origens.append("medida")
            continue
        estimada = None
        if getattr(r, "codigo", None):
            try:
                estimada = vazao_estimada(r.codigo, getattr(r, "nivel_cm", None))
            except Exception:
                estimada = None
        if estimada:
            vazoes.append(estimada["vazao_m3s"])
            origens.append("curva-chave")
        else:
            vazoes.append(None)
            origens.append(None)
    df["vazao_m3s"] = vazoes
    df["origem_vazao"] = origens

    presentes = [c for c in CAMPOS_UNIFICADOS if c in df.columns]
    return df[presentes]


def exportar_visao_unificada(pasta: str | Path = "dados") -> Path:
    """Publica a visão unificada junto com o snapshot, em CSV."""
    destino = Path(pasta)
    destino.mkdir(parents=True, exist_ok=True)
    caminho = destino / "estacoes_unificado.csv"
    visao_unificada().to_csv(caminho, index=False, encoding="utf-8")
    return caminho


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
    ap.add_argument("--historico", action="store_true",
                    help="baixar a serie historica diaria da ANA (anos de dado)")
    ap.add_argument("--anos", type=int, default=15, help="quantos anos de historico")
    ap.add_argument("--importar-arquivo", action="store_true",
                    help="recarregar dados/serie/*.csv.gz para o banco (maquina nova)")
    ap.add_argument("--inmet", metavar="ANOS",
                    help="baixar a chuva horaria historica do INMET (ex.: 2023,2024)")
    ap.add_argument("--sem-arquivo", action="store_true",
                    help="exportar so o snapshot, sem reescrever dados/serie/*.csv.gz")
    ap.add_argument("--minimo", type=int, default=100,
                    help="minimo de estacoes para a rodada valer (abaixo disso, "
                         "falha e nao exporta)")
    args = ap.parse_args()

    if args.historico:
        r = coletar_historico(
            anos_atras=args.anos,
            progresso=lambda f, m: print(f"[{f:5.0%}] {m}"),
        )
        print(f"{r['registros']:,} registros históricos de {r['estacoes']} estações")
        if r["erros"]:
            print(f"{len(r['erros'])} avisos:", r["erros"][:5])
        raise SystemExit(0)

    if args.inmet:
        anos = tuple(int(a) for a in args.inmet.split(",") if a.strip())
        r = coletar_historico_inmet(anos=anos,
                                    progresso=lambda f, m: print(f"[{f:5.0%}] {m}"))
        print(f"\n{r['registros']:,} registros horarios de chuva, "
              f"{r['estacoes']} estacoes do INMET")
        if r["erros"]:
            print("avisos:", r["erros"])
        raise SystemExit(0)

    if args.importar_arquivo:
        # Ação isolada: reconstruir o banco a partir do arquivo versionado,
        # sem coletar. É o passo de máquina nova, depois do `git clone`.
        n = importar_series_arquivadas()
        print(f"{n:,} registros históricos recarregados de dados/serie/")
        raise SystemExit(0)

    # --- COLHEITA, COM A DISTINÇÃO QUE IMPORTA
    #
    # Falha de rede pontual não é a fonte quebrada. A rodada das 23h21 de
    # 12/08/2026 morreu inteira por uma exceção que escapou, enquanto as
    # rodadas antes e depois passaram com o mesmo código — e o alarme que
    # chegou ao usuário foi idêntico ao de um problema real.
    #
    # Mas silenciar tudo seria repetir o erro do `circleMarker`, quando o
    # coletor devolveu zero estação relatando "0 erros" por três dias. O
    # critério, então, não é a exceção: é a COLHEITA. Voltou dado plausível,
    # a rodada vale mesmo com erros; voltou pouco ou nada, falha alto.
    try:
        resumo = sincronizar(
            incluir_ana=not args.sem_ana,
            progresso=lambda f, m: print(f"[{f:5.0%}] {m}"),
        )
    except Exception as erro:
        print(f"\nFALHA NA COLETA: {type(erro).__name__}: {erro}")
        raise SystemExit(1) from erro

    print(json.dumps({k: v for k, v in resumo.items() if k != "erros"}, indent=2, default=str))
    if resumo["erros"]:
        print(f"\n{len(resumo['erros'])} avisos (primeiros 10):")
        for e in resumo["erros"][:10]:
            print("  -", e)

    if resumo.get("colheita_curta"):
        print()
        print("!" * 72)
        for aviso in resumo["colheita_curta"]:
            print("  " + aviso)
        print("  A remocao de orfas foi PULADA para nao apagar a rede.")
        print("!" * 72)

    colhidas = int(resumo.get("estacoes") or 0)
    if colhidas < args.minimo:
        print(
            f"\nFALHA: {colhidas} estações colhidas, abaixo do mínimo de "
            f"{args.minimo}. O banco NÃO foi exportado — publicar um snapshot "
            f"quase vazio apagaria o bom que já está versionado."
        )
        raise SystemExit(1)

    if args.exportar:
        for caminho in exportar_snapshot():
            print("exportado:", caminho)
        print("exportado:", exportar_visao_unificada())
        # O arquivo mensal e .csv.gz: binario, que o Git nao consegue
        # delta-comprimir. Reescrevendo a cada 15 min o repositorio incharia
        # rapido, entao ele fica para as rodadas espacadas.
        if not args.sem_arquivo:
            for caminho in exportar_series_mensais():
                print("arquivado:", caminho)

    if resumo["erros"]:
        print(
            f"\nRodada válida com {colhidas} estações e "
            f"{len(resumo['erros'])} avisos — erros pontuais de fonte não "
            f"invalidam a colheita."
        )
