"""
GeoRisk-RS — CAMADA GEOESPACIAL
===============================
Substitui os polígonos esquemáticos (senos e cossenos) por geometria OFICIAL, e
substitui o Curve Number chutado por um CN derivado de solo e uso da terra reais.

TRÊS FONTES, TODAS PÚBLICAS E SEM TOKEN
---------------------------------------
1) MANCHAS OFICIAIS DO SGB (geoportal.sgb.gov.br)
   Serviços ArcGIS REST por trás dos visualizadores que o SACE embute na aba
   "Manchas de Inundação". Devolvem GeoJSON em WGS84, prontos para o folium.
   São INDEXADAS POR COTA em centímetros — a mesma grandeza que o banco já mede
   de 15 em 15 minutos. Então escolher a mancha é uma busca, não um cálculo:
   Montenegro medindo 765 cm -> mancha "cota 750 cm" -> área realmente alagada.
   Elaboração: IPH-UFRGS. Cobre 5 municípios do RS, 68 manchas.

2) MANCHAS DA DEFESA CIVIL DO RS (Google My Maps)
   A Defesa Civil publica 60 municípios, mas em PDF — inútil como geometria.
   A exceção são 7 municípios do Vale do Taquari com mapa no Google My Maps,
   que exporta KML. São manchas de EVENTO (a cheia de 22/07/2026), não
   indexadas por cota: servem como camada de referência histórica, não como
   projeção. Cobrem justamente Estrela, Muçum, Encantado e Santa Tereza, que
   o SGB não cobre.

3) SOLO E USO DA TERRA DO IBGE (geoservicos.ibge.gov.br)
   - `CREN:PedologiaSG22/SH21/SH22/SI22` — as quatro cartas que cobrem o RS.
     Dão a ordem e a subordem do solo, de onde sai o grupo hidrológico (A a D),
     que é a capacidade de infiltração.
   - `CREN:Cobertura_uso_terra_2020_RS_serie_revisada` — uso e cobertura.
   Cruzando os dois pela tabela SCS sai o CN real da bacia.

   O polígono da bacia vem do próprio SACE (`bacia_<nome>_shape.json`), o que
   evita ter que delinear bacia a partir de modelo de elevação.

POR QUE ISSO IMPORTA
--------------------
Antes, `georisk_hidrologia` usava CN=75 fixo para o estado inteiro, e com as
72 h acumuladas altas todas as estações iam para AMC III e CN 87,3 — bacia de
basalto raso na Serra e planície arenosa no Uruguai respondendo igual. Agora
cada bacia tem o seu.

O QUE CONTINUA SENDO PARÂMETRO (não é medição)
----------------------------------------------
As duas tabelas de conversão abaixo: ordem de solo -> grupo hidrológico
(GRUPO_HIDROLOGICO) e uso da terra x grupo -> CN (CN_POR_USO). A primeira segue
a classificação hidrológica de solos brasileiros (Sartori, Lombardi Neto &
Genovez, 2005); a segunda são os valores clássicos do SCS/NRCS TR-55 casados
com as classes do IBGE. São escolhas defensáveis e documentadas, não medições —
por isso ficam expostas e editáveis aqui.

CACHE
-----
Tudo é gravado no mesmo `georisk_rs.db`. Mancha de 3 MB e recorte de solo não
são baixados a cada rerun do Streamlit: baixa uma vez, reusa.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
import time
from contextlib import contextmanager
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CAMINHO_BANCO_PADRAO = str(Path(__file__).resolve().parent / "georisk_rs.db")

# -----------------------------------------------------------------------------
# ENDEREÇOS
# -----------------------------------------------------------------------------
SGB_MASTER = (
    "https://geoportal.sgb.gov.br/server/rest/services/hidrologia/"
    "BACIA_DO_CAI_MONTENEGRO/MapServer"
)
# O serviço acima é o mestre nacional; filtramos só as bacias do RS.
BACIAS_RS_NO_SGB = {"BACIA DO TAQUARI", "BACIA CAI", "BACIA URUGUAI"}

SACE_SHAPES = "https://www.sgb.gov.br/sace/sace_nivel/api/dados/"
BACIAS_SACE = {
    "taquari": "Bacia do Taquari-Antas",
    "cai": "Bacia do Rio Caí",
    "uruguai": "Bacia do Rio Uruguai",
    "guaiba": "Bacia do Guaíba",
}

IBGE_WFS = "https://geoservicos.ibge.gov.br/geoserver/wfs"
CARTAS_PEDOLOGIA = (
    "CREN:PedologiaSG22",
    "CREN:PedologiaSH21",
    "CREN:PedologiaSH22",
    "CREN:PedologiaSI22",
)
CAMADA_USO_TERRA = "CREN:Cobertura_uso_terra_2020_RS_serie_revisada"

# Manchas do evento de 22/07/2026 publicadas pela Defesa Civil do RS.
MANCHAS_DEFESA_CIVIL = {
    "Arroio do Meio": "1zbVsbwuDmSwpA8P4jMmPiDhF_G-D4KE",
    "Lajeado": "1HjAioP9Mf1L0ZAKojfMo4BP4x0nV-5c",
    "Encantado": "14BwR3eGDBugxhaQ9OX8mMKItCOOvDVA",
    "Santa Tereza": "1apeF5vTcpg0K8SpVY13Nfr7aKVdFCM4",
    "Cruzeiro do Sul": "1Lbny6uz0AlKt9pgpJF9sKv6j3aAvEaY",
    "Roca Sales": "1QMIXwfb0Smrud5GvBPvAFa6gCkwlB_0",
    "Muçum": "1VlkT-m1d9cS6FAKcbfI2eD3QGN8hjiA",
}
EVENTO_DEFESA_CIVIL = "2026-07-22"

TEMPO_LIMITE = 600
CABECALHO = {"User-Agent": "Mozilla/5.0 (GeoRisk-RS/geo)"}


# -----------------------------------------------------------------------------
# TABELAS DE CONVERSÃO (parâmetros documentados, não medições)
# -----------------------------------------------------------------------------
# Ordem/subordem do solo -> grupo hidrológico (A = infiltra muito, D = quase
# nada). Segue Sartori, Lombardi Neto & Genovez (2005).
GRUPO_HIDROLOGICO = {
    "LATOSSOLO": "A",
    "NITOSSOLO": "B",
    "ARGISSOLO": "B",
    "CHERNOSSOLO": "C",
    "CAMBISSOLO": "C",
    "LUVISSOLO": "C",
    "GLEISSOLO": "D",
    "PLANOSSOLO": "D",
    "ESPODOSSOLO": "D",
    "ORGANOSSOLO": "D",
    "PLINTOSSOLO": "D",
    "VERTISSOLO": "D",
    "DUNAS": "A",
    "AFLORAMENTOS DE ROCHAS": "D",
}
# Neossolo varia demais para um valor só: o Quartzarênico é arenoso e drena,
# o Litólico é raso sobre rocha e escoa quase tudo. Usamos a subordem.
GRUPO_NEOSSOLO = {
    "QUARTZARÊNICO": "A",
    "REGOLÍTICO": "B",
    "FLÚVICO": "C",
    "LITÓLICO": "D",
}
GRUPO_NEOSSOLO_PADRAO = "D"  # o Litólico domina a Serra gaúcha

# Curve Number por classe de uso do IBGE x grupo hidrológico (SCS/NRCS TR-55).
CN_POR_USO = {
    "Área Artificial": {"A": 77, "B": 85, "C": 90, "D": 92},
    "Área Agrícola": {"A": 67, "B": 78, "C": 85, "D": 89},
    "Pastagem com Manejo": {"A": 39, "B": 61, "C": 74, "D": 80},
    "Vegetação Campestre": {"A": 49, "B": 69, "C": 79, "D": 84},
    "Vegetação Florestal": {"A": 30, "B": 55, "C": 70, "D": 77},
    "Silvicultura": {"A": 36, "B": 60, "C": 73, "D": 79},
    "Mosaico de Ocupações em Área Florestal": {"A": 43, "B": 65, "C": 76, "D": 82},
    "Mosaico de Ocupações em Área Campestre": {"A": 55, "B": 70, "C": 80, "D": 85},
    "Área Descoberta": {"A": 77, "B": 86, "C": 91, "D": 94},
    "Corpo d´Água Continental": {"A": 100, "B": 100, "C": 100, "D": 100},
    "Corpo d´Água Costeiro": {"A": 100, "B": 100, "C": 100, "D": 100},
}
GRUPO_PADRAO = "C"   # quando o ponto cai fora de qualquer polígono de solo
CN_PADRAO_CLASSE = 75


# -----------------------------------------------------------------------------
# HTTP E BANCO
# -----------------------------------------------------------------------------
def _sessao() -> requests.Session:
    s = requests.Session()
    s.headers.update(CABECALHO)
    s.verify = False
    return s


@contextmanager
def _conectar(db_path: str = CAMINHO_BANCO_PADRAO):
    """Conexão que fecha ao sair do bloco — ver `georisk_dados.conectar`.

    O `with sqlite3.connect(...)` faz commit mas não fecha, e o projeto abre
    conexão em quase toda função de leitura.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"Banco não encontrado em {db_path}. Rode a coleta primeiro: "
            "`python georisk_dados.py --exportar`."
        )
    con = sqlite3.connect(db_path, timeout=30)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()




def criar_schema_geo(db_path: str = CAMINHO_BANCO_PADRAO) -> None:
    with _conectar(db_path) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS mancha_oficial (
                id          TEXT PRIMARY KEY,   -- '<fonte>|<municipio>|<chave>'
                fonte       TEXT,               -- 'SGB/IPH-UFRGS' | 'Defesa Civil RS'
                bacia       TEXT,
                municipio   TEXT,
                tipo        TEXT,               -- 'cota' | 'evento' | 'retorno'
                cota_cm     REAL,               -- NULL quando é evento
                rotulo      TEXT,
                geojson     TEXT,
                url_origem  TEXT,
                baixado_em  TEXT
            );

            CREATE TABLE IF NOT EXISTS bacia_geom (
                bacia      TEXT PRIMARY KEY,
                nome       TEXT,
                geojson    TEXT,
                baixado_em TEXT
            );

            CREATE TABLE IF NOT EXISTS cn_bacia (
                bacia          TEXT PRIMARY KEY,
                cn_medio       REAL,
                amostras       INTEGER,
                composicao     TEXT,   -- JSON: distribuição de solo e uso
                calculado_em   TEXT
            );

            CREATE TABLE IF NOT EXISTS drenagem_cache (
                chave     TEXT PRIMARY KEY,   -- lat_lon_raio arredondados
                geojson   TEXT,
                obtido_em TEXT
            );

            CREATE TABLE IF NOT EXISTS caracterizacao_bacia (
                bacia        TEXT PRIMARY KEY,
                dados        TEXT,   -- JSON: litologia, estruturas, geomorfologia
                calculado_em TEXT
            );

            -- Bacia oficial de cada estação, por ponto-em-polígono contra o
            -- shapefile das 26 bacias do RS. Ver `atribuir_bacias_oficiais`.
            CREATE TABLE IF NOT EXISTS bacia_oficial (
                id_estacao   TEXT PRIMARY KEY,
                bacia        TEXT,
                area_km2     REAL,
                atribuido_em TEXT
            );

            CREATE TABLE IF NOT EXISTS bacia_poligono (
                nome       TEXT PRIMARY KEY,
                area_km2   REAL,
                geojson    TEXT,
                obtido_em  TEXT
            );

            CREATE TABLE IF NOT EXISTS area_drenagem (
                id_estacao   TEXT PRIMARY KEY,
                codigo_ana   TEXT,
                area_km2     REAL,
                casamento    TEXT,   -- como o código foi casado com o da ANA
                obtido_em    TEXT
            );

            CREATE INDEX IF NOT EXISTS ix_mancha_mun ON mancha_oficial (municipio, cota_cm);
            """
        )


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalizar(texto: str | None) -> str:
    """Compara nome de município ignorando acento, caixa e pontuação."""
    if not texto:
        return ""
    tabela = str.maketrans("ÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇáàâãäéèêëíìîïóòôõöúùûüç",
                           "AAAAAEEEEIIIIOOOOOUUUUCaaaaaeeeeiiiiooooouuuuc")
    return re.sub(r"[^A-Z0-9]", "", texto.translate(tabela).upper())


# -----------------------------------------------------------------------------
# 1. MANCHAS OFICIAIS DO SGB (indexadas por cota)
# -----------------------------------------------------------------------------
RE_COTA = re.compile(r"cota[_\s]*0*(\d+)\s*cm", re.I)


def inventario_sgb() -> list[dict]:
    """Descobre as manchas do RS percorrendo a árvore de grupos do serviço.

    Preferimos descoberta em tempo de execução a fixar os 68 identificadores:
    se o SGB reorganizar o serviço, isto continua funcionando.
    A hierarquia é `BACIA / MUNICÍPIO / MODELAGEM|EVENTOS / cota X cm`.
    """
    dados = _sessao().get(f"{SGB_MASTER}?f=json", timeout=TEMPO_LIMITE).json()
    por_id = {c["id"]: c for c in dados.get("layers", [])}

    def ancestrais(cid: int) -> list[str]:
        cadeia, atual = [], cid
        while atual is not None and atual >= 0:
            cadeia.append(por_id[atual]["name"])
            atual = por_id[atual].get("parentLayerId")
        return list(reversed(cadeia))

    achados = []
    for camada in dados.get("layers", []):
        if camada.get("subLayerIds"):
            continue  # é grupo, não mancha
        caminho = ancestrais(camada["id"])
        if len(caminho) < 4 or caminho[0] not in BACIAS_RS_NO_SGB:
            continue
        m = RE_COTA.search(camada["name"])
        if not m:
            continue
        achados.append(
            {
                "layer_id": camada["id"],
                "bacia": caminho[0].title(),
                "municipio": caminho[1].title(),
                "tipo": "evento" if "EVENTO" in caminho[2].upper() else "cota",
                "cota_cm": float(m.group(1)),
                "rotulo": camada["name"],
            }
        )
    return achados


def baixar_manchas_sgb(db_path: str = CAMINHO_BANCO_PADRAO, progresso=None) -> int:
    """Baixa e cacheia todas as manchas do RS. Pula o que já está no banco."""
    criar_schema_geo(db_path)
    itens = inventario_sgb()
    sessao = _sessao()
    novas = 0

    with _conectar(db_path) as con:
        existentes = {r[0] for r in con.execute("SELECT id FROM mancha_oficial")}

    for i, item in enumerate(itens, 1):
        chave = f"SGB|{item['municipio']}|{int(item['cota_cm'])}"
        if chave in existentes:
            continue
        url = f"{SGB_MASTER}/{item['layer_id']}/query"
        if progresso:
            progresso(i / len(itens),
                      f"SGB: {item['municipio']} — {item['rotulo']} ({i}/{len(itens)})")
        try:
            r = sessao.get(
                url,
                params={"where": "1=1", "outFields": "*", "outSR": "4326", "f": "geojson"},
                timeout=TEMPO_LIMITE,
            )
            geo = r.json()
            if not geo.get("features"):
                continue
        except Exception:
            continue

        with _conectar(db_path) as con:
            con.execute(
                "INSERT OR REPLACE INTO mancha_oficial "
                "(id,fonte,bacia,municipio,tipo,cota_cm,rotulo,geojson,url_origem,baixado_em) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (chave, "SGB/IPH-UFRGS", item["bacia"], item["municipio"], item["tipo"],
                 item["cota_cm"], item["rotulo"], json.dumps(geo), url, _agora()),
            )
        novas += 1
    return novas


# -----------------------------------------------------------------------------
# 2. MANCHAS DA DEFESA CIVIL (KML de evento)
# -----------------------------------------------------------------------------
def _kml_para_geojson(kml: str) -> dict:
    """Converte KML em GeoJSON sem depender de biblioteca geoespacial.

    O My Maps entrega um único Placemark com dezenas de <Polygon>; lemos os
    anéis externos e internos de cada um e montamos um MultiPolygon.
    """
    raiz = ET.fromstring(kml)

    def anel(elemento) -> list[list[float]]:
        texto = (elemento.text or "").strip()
        pontos = []
        for trio in texto.split():
            partes = trio.split(",")
            if len(partes) >= 2:
                pontos.append([float(partes[0]), float(partes[1])])
        return pontos

    poligonos = []
    for pol in raiz.iter():
        if not pol.tag.endswith("Polygon"):
            continue
        externo, internos = None, []
        for filho in pol.iter():
            if not filho.tag.endswith("coordinates"):
                continue
            pontos = anel(filho)
            if len(pontos) < 4:
                continue
            pai = "outer"
            if externo is not None:
                pai = "inner"
            if pai == "outer":
                externo = pontos
            else:
                internos.append(pontos)
        if externo:
            poligonos.append([externo] + internos)

    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "MultiPolygon", "coordinates": poligonos}}
        ],
    }


def baixar_manchas_defesa_civil(db_path: str = CAMINHO_BANCO_PADRAO, progresso=None) -> int:
    criar_schema_geo(db_path)
    sessao = _sessao()
    novas = 0
    total = len(MANCHAS_DEFESA_CIVIL)

    with _conectar(db_path) as con:
        existentes = {r[0] for r in con.execute("SELECT id FROM mancha_oficial")}

    for i, (municipio, mid) in enumerate(MANCHAS_DEFESA_CIVIL.items(), 1):
        chave = f"DC|{municipio}|{EVENTO_DEFESA_CIVIL}"
        if chave in existentes:
            continue
        url = f"https://www.google.com/maps/d/kml?mid={mid}&forcekml=1"
        if progresso:
            progresso(i / total, f"Defesa Civil: {municipio} ({i}/{total})")
        try:
            r = sessao.get(url, timeout=TEMPO_LIMITE)
            geo = _kml_para_geojson(r.content.decode("utf-8", "replace"))
            if not geo["features"][0]["geometry"]["coordinates"]:
                continue
        except Exception:
            continue

        with _conectar(db_path) as con:
            con.execute(
                "INSERT OR REPLACE INTO mancha_oficial "
                "(id,fonte,bacia,municipio,tipo,cota_cm,rotulo,geojson,url_origem,baixado_em) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (chave, "Defesa Civil RS", "Bacia do Taquari-Antas", municipio, "evento",
                 None, f"Evento de {EVENTO_DEFESA_CIVIL}", json.dumps(geo), url, _agora()),
            )
        novas += 1
    return novas


# -----------------------------------------------------------------------------
# 3. CONSULTA DAS MANCHAS (é o que a interface usa)
# -----------------------------------------------------------------------------
def municipios_com_mancha(db_path: str = CAMINHO_BANCO_PADRAO) -> list[dict]:
    criar_schema_geo(db_path)
    with _conectar(db_path) as con:
        linhas = con.execute(
            "SELECT municipio, fonte, tipo, COUNT(*), MIN(cota_cm), MAX(cota_cm) "
            "FROM mancha_oficial GROUP BY municipio, fonte, tipo ORDER BY municipio"
        ).fetchall()
    return [
        {"municipio": m, "fonte": f, "tipo": t, "manchas": n,
         "cota_min_cm": lo, "cota_max_cm": hi}
        for m, f, t, n, lo, hi in linhas
    ]


def mancha_para_cota(
    municipio: str, cota_cm: float | None, db_path: str = CAMINHO_BANCO_PADRAO
) -> dict | None:
    """A mancha oficial que corresponde ao nível medido agora.

    Escolhe a MAIOR cota mapeada que não passa da cota medida — é a
    interpretação conservadora e correta: a área alagada em 765 cm contém a
    área de 750 cm, então mostrar a de 750 não exagera o risco.

    Devolve None quando o município não tem mancha, ou quando o nível está
    abaixo da menor cota mapeada (aí não há o que desenhar).
    """
    if cota_cm is None:
        return None
    alvo = _normalizar(municipio)
    criar_schema_geo(db_path)
    # DOIS PASSOS DE PROPÓSITO: primeiro só os metadados (leves), e só depois o
    # GeoJSON da única mancha escolhida. Buscar `geojson` junto com o filtro
    # traria os megabytes de TODAS as manchas candidatas a cada chamada —
    # centenas de MB por rerun do Streamlit.
    with _conectar(db_path) as con:
        candidatas = con.execute(
            # O critério é ter COTA, não o grupo em que o SGB arquivou:
            # as manchas de Uruguaiana ficam sob "EVENTOS" mas são indexadas
            # por cota (833, 952, 1205 e 1252 cm) e servem igual.
            "SELECT id, municipio, fonte, rotulo, cota_cm, url_origem "
            "FROM mancha_oficial WHERE cota_cm IS NOT NULL AND cota_cm <= ? "
            "ORDER BY cota_cm DESC",
            (cota_cm,),
        ).fetchall()

        for ident, mun, fonte, rotulo, cota, url in candidatas:
            if _normalizar(mun) != alvo:
                continue
            geo = con.execute(
                "SELECT geojson FROM mancha_oficial WHERE id=?", (ident,)
            ).fetchone()[0]
            return {"municipio": mun, "fonte": fonte, "rotulo": rotulo,
                    "cota_cm": cota, "geojson": json.loads(geo), "url_origem": url}
    return None


def mancha_de_evento(
    municipio: str, db_path: str = CAMINHO_BANCO_PADRAO
) -> dict | None:
    """Mancha do evento de referência da Defesa Civil, se houver."""
    alvo = _normalizar(municipio)
    criar_schema_geo(db_path)
    with _conectar(db_path) as con:
        candidatas = con.execute(
            "SELECT id, municipio, fonte, rotulo, url_origem "
            "FROM mancha_oficial WHERE tipo='evento' AND cota_cm IS NULL"
        ).fetchall()
        for ident, mun, fonte, rotulo, url in candidatas:
            if _normalizar(mun) != alvo:
                continue
            geo = con.execute(
                "SELECT geojson FROM mancha_oficial WHERE id=?", (ident,)
            ).fetchone()[0]
            return {"municipio": mun, "fonte": fonte, "rotulo": rotulo,
                    "geojson": json.loads(geo), "url_origem": url}
    return None


# -----------------------------------------------------------------------------
# 4. POLÍGONO DA BACIA
# -----------------------------------------------------------------------------
def baixar_bacia(bacia: str, db_path: str = CAMINHO_BANCO_PADRAO) -> dict | None:
    """Polígono da bacia, direto do SACE — evita delinear bacia a partir de MDE."""
    criar_schema_geo(db_path)
    with _conectar(db_path) as con:
        linha = con.execute(
            "SELECT geojson FROM bacia_geom WHERE bacia=?", (bacia,)
        ).fetchone()
    if linha:
        return json.loads(linha[0])

    try:
        r = _sessao().get(f"{SACE_SHAPES}bacia_{bacia}_shape.json", timeout=TEMPO_LIMITE)
        geo = r.json()
    except Exception:
        return None

    with _conectar(db_path) as con:
        con.execute(
            "INSERT OR REPLACE INTO bacia_geom (bacia,nome,geojson,baixado_em) VALUES (?,?,?,?)",
            (bacia, BACIAS_SACE.get(bacia, bacia), json.dumps(geo), _agora()),
        )
    return geo


# -----------------------------------------------------------------------------
# 5. CURVE NUMBER REAL — solo (IBGE) x uso da terra (IBGE) dentro da bacia
# -----------------------------------------------------------------------------
def _grupo_do_solo(props: dict) -> str | None:
    """Ordem/subordem do solo -> grupo hidrológico A..D.

    Polígonos sem `Ordem` são 'Área urbana' e 'Corpos d'água' do mapa
    pedológico — não são solo, então devolvem None e ficam por conta da camada
    de uso da terra (que já tem CN próprio para urbano e água).
    """
    ordem = (props.get("Ordem") or "").strip().upper()
    if not ordem:
        return None
    if ordem == "NEOSSOLO":
        sub = (props.get("Subordem") or "").strip().upper()
        return GRUPO_NEOSSOLO.get(sub, GRUPO_NEOSSOLO_PADRAO)
    return GRUPO_HIDROLOGICO.get(ordem)


TETO_WFS = 50000  # feições por requisição; acima disso o servidor trunca calado


def _wfs_bruto(camada: str, bbox: tuple[float, float, float, float],
               propriedades: str | None = None) -> list[dict]:
    parametros = {
        "service": "WFS", "version": "1.1.0", "request": "GetFeature",
        "typeName": camada, "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "bbox": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]},EPSG:4326",
        "maxFeatures": str(TETO_WFS),
    }
    if propriedades:
        parametros["propertyName"] = propriedades
    r = _sessao().get(IBGE_WFS, params=parametros, timeout=TEMPO_LIMITE)
    return r.json().get("features", [])


def _wfs_geojson(camada: str, bbox: tuple[float, float, float, float],
                 propriedades: str | None = None, progresso=None) -> dict:
    """GetFeature com LADRILHAMENTO ADAPTATIVO.

    O GeoServer trunca a resposta em silêncio ao bater o `maxFeatures`: não há
    erro, só faltam feições. Isso aconteceu de verdade na bacia do Uruguai, que
    é enorme — 53 % dos pontos de amostragem ficaram "sem classificação" de uso
    da terra e o CN saiu enviesado.

    A correção é dividir a área em ladrilhos e subdividir de novo qualquer
    ladrilho que ainda volte cheio, até nenhum bater no teto.
    """
    minx, miny, maxx, maxy = bbox
    pendentes = [(minx, miny, maxx, maxy)]
    feicoes: dict[str, dict] = {}
    processados = 0

    while pendentes:
        caixa = pendentes.pop()
        try:
            achadas = _wfs_bruto(camada, caixa, propriedades)
        except Exception:
            continue
        processados += 1

        if len(achadas) >= TETO_WFS:
            # Veio cheio: provavelmente truncado. Quebra em quatro e refaz.
            cx = (caixa[0] + caixa[2]) / 2
            cy = (caixa[1] + caixa[3]) / 2
            largura = caixa[2] - caixa[0]
            if largura > 0.02:  # evita subdividir para sempre
                pendentes += [
                    (caixa[0], caixa[1], cx, cy), (cx, caixa[1], caixa[2], cy),
                    (caixa[0], cy, cx, caixa[3]), (cx, cy, caixa[2], caixa[3]),
                ]
                continue

        for f in achadas:
            chave = str(f.get("id") or f.get("properties", {}).get("id") or id(f))
            feicoes[chave] = f
        if progresso:
            progresso(f"{camada.split(':')[-1][:22]}: {len(feicoes)} feições "
                      f"({processados} consultas, {len(pendentes)} pendentes)")

    return {"type": "FeatureCollection", "features": list(feicoes.values())}


def calcular_cn_bacia(
    bacia: str,
    db_path: str = CAMINHO_BANCO_PADRAO,
    lado_grade: int = 90,
    progresso=None,
    recalcular: bool = False,
) -> dict | None:
    """CN médio da bacia, por amostragem em grade regular.

    POR QUE AMOSTRAGEM E NÃO INTERSEÇÃO DE POLÍGONOS
    Cruzar 48 mil polígonos de uso com 4 mil de solo e mais o contorno da bacia
    é caro e frágil (geometrias inválidas, slivers). Uma grade regular dentro da
    bacia dá a MESMA média ponderada por área — porque pontos equiespaçados
    representam áreas iguais — em uma fração do tempo, usando índice espacial.

    Devolve o CN médio e a composição (quanto de cada solo e de cada uso), que
    é o que permite auditar o número em vez de aceitá-lo de olhos fechados.
    """
    from shapely.geometry import shape, Point
    from shapely.prepared import prep
    from shapely.strtree import STRtree

    criar_schema_geo(db_path)
    if not recalcular:
        with _conectar(db_path) as con:
            linha = con.execute(
                "SELECT cn_medio, amostras, composicao, calculado_em FROM cn_bacia WHERE bacia=?",
                (bacia,),
            ).fetchone()
        if linha:
            return {"bacia": bacia, "cn_medio": linha[0], "amostras": linha[1],
                    "composicao": json.loads(linha[2]), "calculado_em": linha[3],
                    "do_cache": True}

    def avisar(f, m):
        if progresso:
            try:
                progresso(f, m)
            except Exception:
                pass

    avisar(0.05, f"Baixando contorno da bacia {bacia}…")
    geo_bacia = baixar_bacia(bacia, db_path)
    if not geo_bacia or not geo_bacia.get("features"):
        return None

    from shapely.ops import unary_union
    contorno = unary_union([shape(f["geometry"]) for f in geo_bacia["features"]])
    pronto = prep(contorno)
    minx, miny, maxx, maxy = contorno.bounds

    # --- Grade de amostragem restrita ao interior da bacia
    passo_x = (maxx - minx) / lado_grade
    passo_y = (maxy - miny) / lado_grade
    amostras = [
        Point(minx + (i + 0.5) * passo_x, miny + (j + 0.5) * passo_y)
        for i in range(lado_grade) for j in range(lado_grade)
    ]
    amostras = [p for p in amostras if pronto.contains(p)]
    if not amostras:
        return None
    avisar(0.15, f"{len(amostras)} pontos de amostragem dentro da bacia.")

    # --- Uso da terra
    avisar(0.25, "Baixando uso e cobertura da terra (IBGE)…")
    uso = _wfs_geojson(CAMADA_USO_TERRA, (minx, miny, maxx, maxy),
                       progresso=lambda m: avisar(0.3, m))
    geo_uso, classe_uso = [], []
    for f in uso.get("features", []):
        try:
            geo_uso.append(shape(f["geometry"]))
            classe_uso.append(f["properties"].get("classe"))
        except Exception:
            continue
    arvore_uso = STRtree(geo_uso) if geo_uso else None
    avisar(0.5, f"{len(geo_uso)} polígonos de uso da terra.")

    # --- Pedologia (as 4 cartas do RS; o bbox descarta as que não tocam)
    geo_solo, grupo_solo, ordem_solo = [], [], []
    for k, carta in enumerate(CARTAS_PEDOLOGIA):
        avisar(0.55 + 0.08 * k, f"Baixando pedologia {carta.split(':')[1]}…")
        try:
            ped = _wfs_geojson(carta, (minx, miny, maxx, maxy),
                               progresso=lambda m: avisar(0.55 + 0.08 * k, m))
        except Exception:
            continue
        for f in ped.get("features", []):
            grupo = _grupo_do_solo(f["properties"])
            if grupo is None:
                continue
            try:
                geo_solo.append(shape(f["geometry"]))
            except Exception:
                continue
            grupo_solo.append(grupo)
            ordem_solo.append(f["properties"].get("Ordem"))
    arvore_solo = STRtree(geo_solo) if geo_solo else None
    avisar(0.85, f"{len(geo_solo)} polígonos de solo classificados.")

    # --- Litologia, para refinar o grupo hidrológico onde o SOLO É RASO.
    # Em solo profundo quem manda é o solo; em Neossolo Litólico e Cambissolo,
    # a rocha logo abaixo é que decide se a água infiltra ou escorre. Basalto
    # maciço da Serra Geral e arenito Botucatu se comportam de forma oposta.
    avisar(0.87, "Litologia (para solos rasos)…")
    geo_lito, grupo_lito = [], []
    try:
        lito = _wfs_geojson(CAMADA_GEOLOGIA, (minx, miny, maxx, maxy))
        for f in lito.get("features", []):
            grupo = _grupo_da_litologia(f["properties"].get("nm_unidade"))
            if grupo is None:
                continue
            try:
                geo_lito.append(shape(f["geometry"]))
            except Exception:
                continue
            grupo_lito.append(grupo)
    except Exception:
        pass
    arvore_lito = STRtree(geo_lito) if geo_lito else None
    avisar(0.9, f"{len(geo_lito)} polígonos de litologia classificados.")

    # --- Amostragem
    from collections import Counter
    contagem_uso, contagem_grupo, contagem_ordem = Counter(), Counter(), Counter()
    contagem_refino = Counter()
    soma_cn, validos, fora_do_rs = 0.0, 0, 0

    for ponto in amostras:
        classe = None
        if arvore_uso is not None:
            for idx in arvore_uso.query(ponto):
                if geo_uso[idx].contains(ponto):
                    classe = classe_uso[idx]
                    break
        if not classe:
            # Ponto sem uso da terra = ponto FORA do Rio Grande do Sul.
            # A camada do IBGE é estadual e as bacias do Uruguai e do Taquari
            # entram em SC, na Argentina e no Uruguai. Contar esses pontos com
            # um CN padrão contaminaria a média com área que o projeto nem
            # monitora — então eles são descartados, e a cobertura efetiva vai
            # registrada em `composicao` para você poder auditar.
            fora_do_rs += 1
            continue

        # O solo só é consultado para ponto que já sabemos estar no RS —
        # senão o contador de ordens somava pontos depois descartados.
        grupo, ordem_aqui = None, None
        if arvore_solo is not None:
            for idx in arvore_solo.query(ponto):
                if geo_solo[idx].contains(ponto):
                    grupo = grupo_solo[idx]
                    ordem_aqui = ordem_solo[idx]
                    contagem_ordem[ordem_aqui] += 1
                    break

        # Solo raso: a rocha embaixo condiciona. Só nesse caso a litologia entra.
        if ordem_aqui and str(ordem_aqui).upper() in SOLOS_RASOS and arvore_lito is not None:
            for idx in arvore_lito.query(ponto):
                if geo_lito[idx].contains(ponto):
                    if grupo_lito[idx] != grupo:
                        contagem_refino[f"{ordem_aqui}: {grupo}->{grupo_lito[idx]}"] += 1
                    grupo = grupo_lito[idx]
                    break

        grupo_final = grupo or GRUPO_PADRAO
        contagem_grupo[grupo_final] += 1
        contagem_uso[classe] += 1
        soma_cn += CN_POR_USO.get(classe, {}).get(grupo_final, CN_PADRAO_CLASSE)
        validos += 1

    cn_medio = round(soma_cn / validos, 1) if validos else None
    composicao = {
        "uso_da_terra": dict(contagem_uso.most_common()),
        "grupo_hidrologico": dict(contagem_grupo.most_common()),
        "ordem_do_solo": {str(k): v for k, v in contagem_ordem.most_common()},
        "pontos_na_bacia": len(amostras),
        "pontos_no_rs": validos,
        "pontos_fora_do_rs": fora_do_rs,
        "cobertura_rs_pct": round(100 * validos / max(len(amostras), 1), 1),
        "refino_por_litologia": dict(contagem_refino.most_common()),
        "solo_identificado_pct": round(
            100 * sum(contagem_ordem.values()) / max(validos, 1), 1
        ),
    }

    with _conectar(db_path) as con:
        con.execute(
            "INSERT OR REPLACE INTO cn_bacia (bacia,cn_medio,amostras,composicao,calculado_em) "
            "VALUES (?,?,?,?,?)",
            (bacia, cn_medio, validos, json.dumps(composicao, ensure_ascii=False), _agora()),
        )
    avisar(1.0, f"CN da bacia {bacia}: {cn_medio}")
    return {"bacia": bacia, "cn_medio": cn_medio, "amostras": validos,
            "composicao": composicao, "calculado_em": _agora(), "do_cache": False}


# Rótulo da bacia usado em `estacao.bacia` -> chave da bacia no SACE.
_ROTULO_PARA_CHAVE = {_normalizar(v): k for k, v in BACIAS_SACE.items()}


def cn_da_bacia(rotulo_bacia: str | None, db_path: str = CAMINHO_BANCO_PADRAO) -> float | None:
    """CN já calculado para a bacia de uma estação. None se ainda não houver.

    É o gancho que `georisk_hidrologia` usa: se existir CN calculado, ele
    substitui o parâmetro fixo; se não existir, o comportamento antigo continua.
    """
    chave = _ROTULO_PARA_CHAVE.get(_normalizar(rotulo_bacia))
    if not chave:
        return None
    try:
        criar_schema_geo(db_path)
        with _conectar(db_path) as con:
            linha = con.execute(
                "SELECT cn_medio FROM cn_bacia WHERE bacia=?", (chave,)
            ).fetchone()
    except Exception:
        return None
    return linha[0] if linha else None


def cns_calculados(db_path: str = CAMINHO_BANCO_PADRAO) -> list[dict]:
    criar_schema_geo(db_path)
    with _conectar(db_path) as con:
        linhas = con.execute(
            "SELECT bacia, cn_medio, amostras, composicao, calculado_em FROM cn_bacia"
        ).fetchall()
    return [
        {"bacia": b, "nome": BACIAS_SACE.get(b, b), "cn_medio": cn, "amostras": n,
         "composicao": json.loads(c), "calculado_em": q}
        for b, cn, n, c, q in linhas
    ]


# -----------------------------------------------------------------------------
# 6. PREPARAÇÃO COMPLETA
# -----------------------------------------------------------------------------
def preparar_tudo(db_path: str = CAMINHO_BANCO_PADRAO, progresso=None) -> dict:
    """Baixa manchas e calcula o CN das bacias. Idempotente: reusa o cache."""
    criar_schema_geo(db_path)

    def avisar(f, m):
        if progresso:
            try:
                progresso(f, m)
            except Exception:
                pass

    avisar(0.02, "Descobrindo manchas oficiais do SGB…")
    n_sgb = baixar_manchas_sgb(db_path, progresso=lambda f, m: avisar(0.02 + f * 0.38, m))

    avisar(0.42, "Baixando manchas da Defesa Civil…")
    n_dc = baixar_manchas_defesa_civil(
        db_path, progresso=lambda f, m: avisar(0.42 + f * 0.12, m)
    )

    cns, caracterizadas = {}, {}
    bacias = list(BACIAS_SACE)
    fatia = 0.44 / len(bacias)
    for i, bacia in enumerate(bacias):
        base = 0.55 + i * fatia
        avisar(base, f"Calculando CN da bacia {bacia}…")
        try:
            r = calcular_cn_bacia(
                bacia, db_path,
                progresso=lambda f, m: avisar(base + f * fatia * 0.6, m),
            )
            cns[bacia] = r["cn_medio"] if r else None
        except Exception as exc:
            cns[bacia] = None
            avisar(base, f"CN da bacia {bacia} falhou: {type(exc).__name__}")

        # Caracterização geológica junto: a litologia refina o CN em solo raso
        # e a densidade de drenagem afere o Tc. Roda aqui para o usuário não
        # precisar lembrar de um segundo comando.
        avisar(base + fatia * 0.6, f"Caracterizando a bacia {bacia}…")
        try:
            c = caracterizar_bacia(
                bacia, db_path,
                progresso=lambda f, m: avisar(base + fatia * (0.6 + f * 0.4), m),
            )
            caracterizadas[bacia] = (
                c["geomorfologia"]["densidade_dominante"] if c else None
            )
        except Exception as exc:
            caracterizadas[bacia] = None
            avisar(base, f"Caracterização de {bacia} falhou: {type(exc).__name__}")

    avisar(0.97, "Buscando a área de drenagem de cada estação…")
    try:
        areas = atualizar_areas_drenagem(db_path)
        n_areas = int(areas["area_km2"].notna().sum())
    except Exception as exc:
        n_areas = 0
        avisar(0.97, f"Áreas de drenagem falharam: {type(exc).__name__}")

    avisar(1.0, "Camada geoespacial pronta.")
    return {"manchas_sgb_novas": n_sgb, "manchas_defesa_civil_novas": n_dc,
            "cn_por_bacia": cns, "densidade_drenagem_por_bacia": caracterizadas,
            "estacoes_com_area_drenagem": n_areas}


# -----------------------------------------------------------------------------
# 7. GEOLOGIA, ESTRUTURA E GEOMORFOLOGIA (IBGE / BDIA)
# -----------------------------------------------------------------------------
# Três camadas do Banco de Dados de Informações Ambientais do IBGE:
#   BDIA:geol_area          -> unidade litoestratigráfica (Serra Geral, Botucatu…)
#   BDIA:geol_linha_falha   -> falhas, com forma (definida/inferida) e mergulho
#   BDIA:geol_linha_fratura -> fraturas, com mergulho e rocha associada
#   BDIA:geom_area          -> geomorfologia, com DENSIDADE DE DRENAGEM e incisão
#
# ONDE CADA UMA ENTRA — E ONDE NÃO ENTRA
# --------------------------------------
# 1) LITOLOGIA refina o grupo hidrológico de SOLO RASO, e só dele. Um Neossolo
#    Litólico sobre basalto maciço da Serra Geral realmente escoa quase tudo
#    (grupo D); o mesmo Neossolo sobre o arenito Botucatu infiltra muito
#    (grupo A). Aplicar a litologia onde o solo é profundo seria errado: ali
#    quem manda é o solo, não a rocha embaixo.
#
# 2) DENSIDADE DE DRENAGEM serve de AFERIÇÃO do Tc, não de entrada. Bacia com
#    drenagem densa responde mais rápido — é relação consagrada. Usamos para
#    dizer "o Tc medido é compatível com a densidade observada", do mesmo modo
#    que Kirpich serviria se houvesse morfometria. Não realimenta o cálculo.
#
# 3) DENSIDADE DE LINEAMENTOS (falhas + fraturas) fica como CARACTERIZAÇÃO
#    apenas. É proxy reconhecido de permeabilidade secundária, mas NÃO existe
#    coeficiente consagrado que a converta em correção de Curve Number.
#    Inventar esse fator seria repetir exatamente o que este projeto passou a
#    remover. O índice é calculado, exibido e documentado — a interpretação
#    fica com quem tem a referência da área.

CAMADA_GEOLOGIA = "BDIA:geol_area"
CAMADA_FALHAS = "BDIA:geol_linha_falha"
CAMADA_FRATURAS = "BDIA:geol_linha_fratura"
CAMADA_GEOMORFOLOGIA = "BDIA:geom_area"

# Unidade litoestratigráfica -> grupo hidrológico da ROCHA, aplicado só onde o
# solo é raso. Chave por prefixo, porque o IBGE detalha fácies
# ("Serra Geral - Fácies Caxias").
LITOLOGIA_GRUPO = {
    "SERRA GERAL": "D",        # basalto/riodacito maciço: escoa
    "BOTUCATU": "A",           # arenito eólico: muito permeável
    "PIRAMBOIA": "A",
    "ROSARIO DO SUL": "B",     # arenito/siltito
    "SANTA MARIA": "C",
    "RIO DO RASTO": "C",
    "ESTRADA NOVA": "C",
    "IRATI": "D",              # folhelho/siltito: impermeável
    "PALERMO": "C",
    "RIO BONITO": "B",
    "ITARARE": "B",
    "DEPOSITOS ALUVIONARES": "C",
    "DEPOSITOS COLUVIO": "C",
    "GRANITO": "D",
    "GNAISSE": "D",
}

# Solos rasos: são estes que a rocha embaixo condiciona de fato.
SOLOS_RASOS = {"NEOSSOLO", "CAMBISSOLO", "AFLORAMENTOS DE ROCHAS"}

# Densidade de drenagem do IBGE -> faixa de tempo de resposta esperada.
# Serve para conferir o Tc empírico, não para calculá-lo.
DRENAGEM_TC_ESPERADO = {
    "muito alta": (0.5, 6.0),
    "alta": (1.0, 10.0),
    "média": (4.0, 18.0),
    "baixa": (8.0, 30.0),
    "muito baixa": (12.0, 48.0),
}


def _grupo_da_litologia(nome_unidade: str | None) -> str | None:
    if not nome_unidade:
        return None
    alvo = _normalizar(nome_unidade)
    for chave, grupo in LITOLOGIA_GRUPO.items():
        if _normalizar(chave) in alvo:
            return grupo
    return None


def _km_por_grau(latitude: float) -> tuple[float, float]:
    """Conversão local grau->km. Longitude encurta com o cosseno da latitude."""
    import math
    return 111.32 * math.cos(math.radians(latitude)), 110.57


def caracterizar_bacia(
    bacia: str,
    db_path: str = CAMINHO_BANCO_PADRAO,
    lado_grade: int = 70,
    progresso=None,
    recalcular: bool = False,
) -> dict | None:
    """Litologia, estrutura e geomorfologia da bacia.

    Devolve composição litológica, densidade de lineamentos em km/km² e a
    densidade de drenagem dominante, além do intervalo de Tc que essa densidade
    faria esperar — para você confrontar com o Tc que sai da correlação.
    """
    from shapely.geometry import shape, Point
    from shapely.ops import unary_union
    from shapely.prepared import prep
    from shapely.strtree import STRtree

    criar_schema_geo(db_path)
    if not recalcular:
        with _conectar(db_path) as con:
            linha = con.execute(
                "SELECT dados, calculado_em FROM caracterizacao_bacia WHERE bacia=?",
                (bacia,),
            ).fetchone()
        if linha:
            saida = json.loads(linha[0])
            saida["do_cache"] = True
            return saida

    def avisar(f, m):
        if progresso:
            try:
                progresso(f, m)
            except Exception:
                pass

    avisar(0.05, f"Contorno da bacia {bacia}…")
    geo_bacia = baixar_bacia(bacia, db_path)
    if not geo_bacia or not geo_bacia.get("features"):
        return None
    contorno = unary_union([shape(f["geometry"]) for f in geo_bacia["features"]])
    pronto = prep(contorno)
    minx, miny, maxx, maxy = contorno.bounds
    lat_media = (miny + maxy) / 2
    kx, ky = _km_por_grau(lat_media)
    area_km2 = contorno.area * kx * ky

    # --- Grade de amostragem (mesma lógica do CN: pontos equiespaçados = área)
    passo_x = (maxx - minx) / lado_grade
    passo_y = (maxy - miny) / lado_grade
    amostras = [
        Point(minx + (i + 0.5) * passo_x, miny + (j + 0.5) * passo_y)
        for i in range(lado_grade) for j in range(lado_grade)
    ]
    amostras = [p for p in amostras if pronto.contains(p)]
    if not amostras:
        return None

    from collections import Counter

    # --- Litologia
    avisar(0.2, "Litologia (BDIA:geol_area)…")
    geol = _wfs_geojson(CAMADA_GEOLOGIA, (minx, miny, maxx, maxy),
                        progresso=lambda m: avisar(0.25, m))
    poligonos, unidades, tempos = [], [], []
    for f in geol.get("features", []):
        try:
            poligonos.append(shape(f["geometry"]))
        except Exception:
            continue
        unidades.append(f["properties"].get("nm_unidade"))
        tempos.append(f["properties"].get("nm_tempo_g"))
    arvore_geol = STRtree(poligonos) if poligonos else None

    conta_unidade, conta_tempo = Counter(), Counter()
    for ponto in amostras:
        if arvore_geol is None:
            break
        for idx in arvore_geol.query(ponto):
            if poligonos[idx].contains(ponto):
                conta_unidade[unidades[idx]] += 1
                conta_tempo[tempos[idx]] += 1
                break

    # --- Geomorfologia
    avisar(0.5, "Geomorfologia (BDIA:geom_area)…")
    geom = _wfs_geojson(CAMADA_GEOMORFOLOGIA, (minx, miny, maxx, maxy),
                        progresso=lambda m: avisar(0.55, m))
    pol_geom, densidades, incisoes, unid_geom = [], [], [], []
    for f in geom.get("features", []):
        try:
            pol_geom.append(shape(f["geometry"]))
        except Exception:
            continue
        densidades.append(f["properties"].get("dens_dren"))
        incisoes.append(f["properties"].get("aprof_inci"))
        unid_geom.append(f["properties"].get("nm_unidade"))
    arvore_geom = STRtree(pol_geom) if pol_geom else None

    conta_dens, conta_inci, conta_relevo = Counter(), Counter(), Counter()
    for ponto in amostras:
        if arvore_geom is None:
            break
        for idx in arvore_geom.query(ponto):
            if pol_geom[idx].contains(ponto):
                if densidades[idx]:
                    conta_dens[densidades[idx]] += 1
                if incisoes[idx]:
                    conta_inci[incisoes[idx]] += 1
                if unid_geom[idx]:
                    conta_relevo[unid_geom[idx]] += 1
                break

    # --- Estruturas: comprimento DENTRO da bacia, não o comprimento total
    estruturas = {}
    for rotulo, camada in (("falhas", CAMADA_FALHAS), ("fraturas", CAMADA_FRATURAS)):
        avisar(0.7, f"Estruturas: {rotulo}…")
        try:
            linhas = _wfs_geojson(camada, (minx, miny, maxx, maxy),
                                  progresso=lambda m: avisar(0.75, m))
        except Exception:
            estruturas[rotulo] = {"n": 0, "km": 0.0, "km_por_km2": 0.0}
            continue

        comprimento_graus, quantidade = 0.0, 0
        formas, mergulhos = Counter(), Counter()
        for f in linhas.get("features", []):
            try:
                geometria = shape(f["geometry"])
            except Exception:
                continue
            recorte = geometria.intersection(contorno)
            if recorte.is_empty:
                continue
            comprimento_graus += recorte.length
            quantidade += 1
            props = f["properties"]
            if props.get("forma"):
                formas[props["forma"]] += 1
            merg = props.get("estm_merg") or props.get("mergulho")
            if merg:
                mergulhos[merg] += 1

        km = comprimento_graus * ((kx + ky) / 2)
        estruturas[rotulo] = {
            "n": quantidade,
            "km": round(km, 1),
            "km_por_km2": round(km / area_km2, 4) if area_km2 else 0.0,
            "forma": dict(formas.most_common()),
            "mergulho": dict(mergulhos.most_common()),
        }

    densidade_dominante = conta_dens.most_common(1)[0][0] if conta_dens else None
    tc_esperado = DRENAGEM_TC_ESPERADO.get(densidade_dominante)

    total_lineamentos = sum(e["km"] for e in estruturas.values())
    resultado = {
        "bacia": bacia,
        "nome": BACIAS_SACE.get(bacia, bacia),
        "area_km2": round(area_km2, 1),
        "amostras": len(amostras),
        "litologia": {
            "unidades": dict(conta_unidade.most_common()),
            "tempo_geologico": dict(conta_tempo.most_common(6)),
        },
        "geomorfologia": {
            "densidade_drenagem": dict(conta_dens.most_common()),
            "densidade_dominante": densidade_dominante,
            "aprofundamento_incisao": dict(conta_inci.most_common(5)),
            "unidades_relevo": dict(conta_relevo.most_common(5)),
        },
        "estruturas": estruturas,
        "densidade_lineamentos_km_km2": (
            round(total_lineamentos / area_km2, 4) if area_km2 else 0.0
        ),
        "tc_esperado_pela_drenagem_h": tc_esperado,
        "calculado_em": _agora(),
        "do_cache": False,
    }

    with _conectar(db_path) as con:
        con.execute(
            "INSERT OR REPLACE INTO caracterizacao_bacia (bacia,dados,calculado_em) "
            "VALUES (?,?,?)",
            (bacia, json.dumps(resultado, ensure_ascii=False), _agora()),
        )
    avisar(1.0, f"Caracterização da bacia {bacia} concluída.")
    return resultado


def caracterizacoes(db_path: str = CAMINHO_BANCO_PADRAO) -> list[dict]:
    criar_schema_geo(db_path)
    with _conectar(db_path) as con:
        linhas = con.execute("SELECT dados FROM caracterizacao_bacia").fetchall()
    return [json.loads(d[0]) for d in linhas]


def conferir_tc(bacia_rotulo: str | None, tc_horas: float | None,
                db_path: str = CAMINHO_BANCO_PADRAO) -> dict | None:
    """Confronta o Tc empírico com o que a densidade de drenagem faria esperar.

    É AFERIÇÃO, não correção: o Tc continua vindo da correlação cruzada entre
    chuva e subida do rio. Aqui só se diz se ele é compatível com o relevo.
    """
    if tc_horas is None:
        return None
    chave = _ROTULO_PARA_CHAVE.get(_normalizar(bacia_rotulo))
    if not chave:
        return None
    with _conectar(db_path) as con:
        linha = con.execute(
            "SELECT dados FROM caracterizacao_bacia WHERE bacia=?", (chave,)
        ).fetchone()
    if not linha:
        return None

    dados = json.loads(linha[0])
    faixa = dados.get("tc_esperado_pela_drenagem_h")
    if not faixa:
        return None
    minimo, maximo = faixa
    if tc_horas < minimo:
        veredito = "mais rápido que o esperado para esta densidade de drenagem"
    elif tc_horas > maximo:
        veredito = "mais lento que o esperado para esta densidade de drenagem"
    else:
        veredito = "compatível com a densidade de drenagem da bacia"
    return {
        "densidade_drenagem": dados["geomorfologia"]["densidade_dominante"],
        "tc_esperado_h": faixa,
        "tc_medido_h": tc_horas,
        "veredito": veredito,
    }


# -----------------------------------------------------------------------------
# FAIXAS DE RISCO SOBRE A HIDROGRAFIA OFICIAL
# -----------------------------------------------------------------------------
# Só 5 municípios do RS têm mancha modelada pelo SGB/IPH-UFRGS. Para os outros
# não existe mancha oficial — e as estações que mais importam hoje (Estrela,
# Encantado, Muçum, Bom Retiro do Sul) estão justamente entre elas.
#
# A versão original do projeto preenchia esse vazio com senos e cossenos ao
# redor da estação: um rio inventado, que não existe no terreno. Aqui o traçado
# é REAL — vem da hidrografia oficial do IBGE (`BC100_RS_2021_Trecho_Drenagem_L`,
# escala 1:100.000, com os cursos nomeados) — e o que é estimado é apenas a
# LARGURA da faixa.
#
# O QUE É REAL E O QUE É ESTIMADO
# --------------------------------
#   real      : o curso do rio, a posição, o nome, a rede de afluentes
#   real      : as cotas de atenção/alerta/inundação (oficiais do SACE)
#   ESTIMADO  : a largura da faixa em cada cota
#
# A largura NÃO é mancha de inundação modelada. Sem MDE e sem seção
# transversal, não há como derivar até onde a água chega. A faixa é
# proporcional à altura da cota, o que dá noção comparativa entre os três
# níveis de risco — não a extensão real do alagamento.
#
# Por isso as faixas saem sempre rotuladas como estimativa, e onde EXISTE
# mancha oficial ela tem prioridade: o painel desenha a modelada, não esta.

CAMADA_DRENAGEM_RS = "CCAR:BC100_RS_2021_Trecho_Drenagem_L"

# Quantos graus ao redor da estação buscar trechos de rio (~11 km).
RAIO_DRENAGEM_GRAUS = 0.10

# Metros de faixa para cada metro de cota. Valor de ordem de grandeza: numa
# planície de vale encaixado, 1 m de lâmina espalha algumas dezenas de metros
# para cada lado. É PARÂMETRO, não medição.
METROS_POR_METRO_DE_COTA = 40.0

# Teto para a faixa não virar um borrão no mapa.
LARGURA_MAXIMA_M = 2500.0

# Quanto a área "segura" se estende além do pior caso projetado. 1,5 dá uma
# margem de 50 % sobre o limite superior do erro — é folga deliberada, porque
# errar para o lado de chamar de perigoso o que é seguro custa menos que o
# contrário.
MARGEM_AREA_SEGURA = 1.5

# Tolerância de simplificação, em graus (~55 m). Sem isso cada faixa sai com
# dezenas de milhares de vértices: medido, 1 MB de GeoJSON por estação, o que
# dava 44 MB no HTML e travava o navegador antes de desenhar qualquer coisa.
# Numa faixa cuja largura já é estimativa de centenas de metros, 55 m de
# tolerância não muda nada visualmente.
TOLERANCIA_SIMPLIFICACAO = 0.0005

# Só os trechos de rio até esta distância da estação entram na faixa. Buscar
# 11 km ao redor traz a bacia toda de afluentes; o que interessa é o rio na
# vizinhança da estação.
RAIO_FAIXA_GRAUS = 0.045


def _linha_para_pontos(geometria: dict) -> list[list[list[float]]]:
    """Extrai listas de coordenadas de LineString ou MultiLineString."""
    tipo = geometria.get("type")
    if tipo == "LineString":
        return [geometria["coordinates"]]
    if tipo == "MultiLineString":
        return list(geometria["coordinates"])
    return []


def drenagem_proxima(
    lat: float, lon: float, raio_graus: float = RAIO_DRENAGEM_GRAUS,
    db_path: str = CAMINHO_BANCO_PADRAO,
) -> dict:
    """Trechos de rio da hidrografia oficial do IBGE ao redor de um ponto.

    Fica em cache no banco: a hidrografia não muda, e cada consulta ao WFS
    custa alguns segundos.
    """
    criar_schema_geo(db_path)
    chave = f"{round(lat, 2)}_{round(lon, 2)}_{raio_graus}"

    with _conectar(db_path) as con:
        linha = con.execute(
            "SELECT geojson FROM drenagem_cache WHERE chave = ?", (chave,)
        ).fetchone()
    if linha:
        return json.loads(linha[0])

    caixa = (lon - raio_graus, lat - raio_graus, lon + raio_graus, lat + raio_graus)
    try:
        dados = _wfs_geojson(CAMADA_DRENAGEM_RS, caixa)
    except Exception:
        dados = {"type": "FeatureCollection", "features": []}

    with _conectar(db_path) as con:
        con.execute(
            "INSERT OR REPLACE INTO drenagem_cache (chave, geojson, obtido_em) "
            "VALUES (?,?,?)",
            (chave, json.dumps(dados), _agora()),
        )
    return dados


def faixas_de_risco(
    lat: float,
    lon: float,
    cotas_cm: dict[str, float],
    nome_rio: str | None = None,
    db_path: str = CAMINHO_BANCO_PADRAO,
) -> list[dict]:
    """Faixas ao longo do rio real, uma por nível de risco.

    `cotas_cm` é {"atencao": x, "alerta": y, "inundacao": z}. Devolve, da maior
    para a menor, dicionários com o polígono em GeoJSON pronto para o folium.

    A geometria da linha é oficial; a largura é estimativa proporcional à cota.
    """
    from shapely.geometry import LineString, mapping
    from shapely.ops import unary_union

    dados = drenagem_proxima(lat, lon, db_path=db_path)
    feicoes = dados.get("features", [])
    if not feicoes:
        return []

    # Prioriza o rio da estação; sem nome, usa toda a drenagem próxima.
    alvo = _normalizar(nome_rio) if nome_rio else None
    linhas = []
    for f in feicoes:
        nome = _normalizar(f.get("properties", {}).get("nome") or "")
        if alvo and nome and alvo not in nome and nome not in alvo:
            continue
        for coords in _linha_para_pontos(f.get("geometry") or {}):
            if len(coords) >= 2:
                linhas.append(LineString(coords))
    if not linhas:  # nenhum trecho com o nome do rio: usa o que houver por perto
        for f in feicoes:
            for coords in _linha_para_pontos(f.get("geometry") or {}):
                if len(coords) >= 2:
                    linhas.append(LineString(coords))
    if not linhas:
        return []

    # Mantém só os trechos perto da estação: a busca traz ~120 segmentos num
    # raio de 11 km, e desenhar toda a rede de afluentes deixa a faixa
    # irreconhecível além de pesada.
    from shapely.geometry import Point
    estacao = Point(lon, lat)
    perto = [l for l in linhas if l.distance(estacao) <= RAIO_FAIXA_GRAUS]
    eixo = unary_union(perto or linhas)
    import math
    graus_por_metro_lat = 1.0 / 110540.0
    graus_por_metro_lon = 1.0 / (111320.0 * max(math.cos(math.radians(lat)), 0.1))
    grau_medio = (graus_por_metro_lat + graus_por_metro_lon) / 2

    ordem = [
        ("inundacao", "Inundação", "#f44336", "#b71c1c"),
        ("alerta", "Alerta", "#ff9800", "#e65100"),
        ("atencao", "Atenção", "#ffeb3b", "#fbc02d"),
    ]

    saida = []
    for chave, rotulo, preenche, borda in ordem:
        cota = cotas_cm.get(chave)
        if cota is None or pd.isna(cota):
            continue
        largura_m = min(
            float(cota) / 100.0 * METROS_POR_METRO_DE_COTA, LARGURA_MAXIMA_M
        )
        poligono = eixo.buffer(largura_m * grau_medio, resolution=4)
        poligono = poligono.simplify(TOLERANCIA_SIMPLIFICACAO, preserve_topology=True)
        if poligono.is_empty:
            continue
        saida.append({
            "nivel": chave,
            "rotulo": rotulo,
            "cota_cm": float(cota),
            "largura_estimada_m": round(largura_m),
            "cor_preenchimento": preenche,
            "cor_borda": borda,
            "geojson": {"type": "Feature", "properties": {},
                        "geometry": mapping(poligono)},
        })
    return saida


def faixas_de_incerteza(
    lat: float,
    lon: float,
    cota_min_cm: float,
    cota_projetada_cm: float,
    cota_max_cm: float,
    nome_rio: str | None = None,
    db_path: str = CAMINHO_BANCO_PADRAO,
) -> list[dict]:
    """Mancha da COTA PROJETADA, com as cores marcando a incerteza.

    Diferente de `faixas_de_risco`, que desenha os limiares fixos de
    atenção/alerta/inundação. Aqui as três manchas saem do envelope da própria
    projeção — mínimo, central e máximo — e a leitura é de confiança:

        vermelho  EXTREMO ALERTA .. a água chega aqui mesmo no cenário otimista
        laranja   ALERTA .......... projeção central do modelo
        amarelo   ATENÇÃO ......... pior caso dentro do erro observado
        verde     SEGURO .......... além do alcance projetado

    A leitura é de fora para dentro: quanto mais perto do rio, maior o risco. O
    verde não é uma cota — é a área que a projeção NÃO alcança, com a margem
    do erro do modelo já somada. Marca até onde se pode considerar seguro
    segundo esta projeção, e é o que dá sentido operacional ao mapa: sem ele o
    usuário não sabe se está fora do alcance ou apenas fora do desenho.

    As larguras vêm do envelope da projeção — mínima, central e máxima — cujo
    espaçamento é o erro típico que o modelo cometeu na validação walk-forward.

    A largura do envelope é o erro típico que o modelo cometeu na validação
    walk-forward, não um intervalo de confiança formal. É o que se pode afirmar
    com os dados: a faixa de erro que ele de fato produziu fora da amostra.

    Desenhadas da maior para a menor, para todas ficarem visíveis.
    """
    from shapely.geometry import LineString, Point, mapping
    from shapely.ops import unary_union
    import math

    dados = drenagem_proxima(lat, lon, db_path=db_path)
    feicoes = dados.get("features", [])
    if not feicoes:
        return []

    alvo = _normalizar(nome_rio) if nome_rio else None
    linhas = []
    for f in feicoes:
        nome = _normalizar(f.get("properties", {}).get("nome") or "")
        if alvo and nome and alvo not in nome and nome not in alvo:
            continue
        for coords in _linha_para_pontos(f.get("geometry") or {}):
            if len(coords) >= 2:
                linhas.append(LineString(coords))
    if not linhas:
        for f in feicoes:
            for coords in _linha_para_pontos(f.get("geometry") or {}):
                if len(coords) >= 2:
                    linhas.append(LineString(coords))
    if not linhas:
        return []

    estacao = Point(lon, lat)
    perto = [l for l in linhas if l.distance(estacao) <= RAIO_FAIXA_GRAUS]
    eixo = unary_union(perto or linhas)

    grau_lat = 1.0 / 110540.0
    grau_lon = 1.0 / (111320.0 * max(math.cos(math.radians(lat)), 0.1))
    grau_medio = (grau_lat + grau_lon) / 2

    # A faixa segura vai ALÉM do pior caso, com margem — é o que sobra fora do
    # alcance projetado. Sem ela o mapa não diz onde é seguro, só onde é
    # perigoso, e quem está na borda do desenho fica sem resposta.
    cota_segura = (
        float(cota_max_cm) * MARGEM_AREA_SEGURA
        if cota_max_cm is not None and not pd.isna(cota_max_cm) else None
    )

    # Ordem FIXA, da maior área para a menor: o verde por baixo e o vermelho
    # por cima. Como min < central < máx < segura por construção, não há risco
    # de uma faixa cobrir a outra.
    niveis = [
        ("segura", "Seguro", cota_segura, "#4caf50", "#1b5e20",
         "além do alcance projetado, com margem do erro do modelo"),
        ("atencao", "Atenção", cota_max_cm, "#ffeb3b", "#fbc02d",
         "pior caso dentro do erro observado"),
        ("alerta", "Alerta", cota_projetada_cm, "#ff9800", "#e65100",
         "projeção central do modelo"),
        ("extremo", "Extremo alerta", cota_min_cm, "#f44336", "#b71c1c",
         "a água chega aqui mesmo no cenário otimista"),
    ]
    niveis = [n for n in niveis
              if n[2] is not None and not pd.isna(n[2]) and float(n[2]) > 0]

    saida = []
    for chave, rotulo, cota, preenche, borda, leitura in niveis:
        largura_m = min(
            float(cota) / 100.0 * METROS_POR_METRO_DE_COTA, LARGURA_MAXIMA_M
        )
        poligono = eixo.buffer(largura_m * grau_medio, resolution=4)
        poligono = poligono.simplify(TOLERANCIA_SIMPLIFICACAO, preserve_topology=True)
        if poligono.is_empty:
            continue
        saida.append({
            "nivel": chave,
            "rotulo": rotulo,
            "leitura": leitura,
            "eh_area_segura": chave == "segura",
            "cota_cm": float(cota),
            "largura_estimada_m": round(largura_m),
            "cor_preenchimento": preenche,
            "cor_borda": borda,
            "geojson": {"type": "Feature", "properties": {},
                        "geometry": mapping(poligono)},
        })
    return saida


# -----------------------------------------------------------------------------
# BACIAS OFICIAIS DO RS (shapefile SEMA/DRH)
# -----------------------------------------------------------------------------
# O projeto vinha rotulando bacia pelo que o SACE publica: quatro nomes, e
# "Não catalogada (ANA)" para 485 das 552 estações — 88 % do cadastro sem
# bacia utilizável. O shapefile oficial das 26 bacias do RS resolve isso por
# ponto-em-polígono: 550 das 552 recebem bacia real, cobrindo as 26.
#
# As duas que ficam de fora são barragens sobre o rio Uruguai exatamente na
# divisa com Santa Catarina (Barra Grande e Foz do Chapecó). Estar fora dos
# polígonos do RS é o comportamento correto ali, não falha de casamento.
#
# CRS do arquivo: SIRGAS 2000 geográfico (EPSG:4674). Em graus, e a diferença
# para WGS84 é inferior a um metro — por isso não há reprojeção, e o projeto
# segue sem depender de pyproj.
#
# LEITURA SEM GEOPANDAS
# ---------------------
# Não há geopandas, fiona, pyshp nem osgeo neste ambiente, e o formato é
# simples o bastante para ler direto. `_ler_dbf` e `_ler_shp_poligonos` abaixo
# fazem isso com `struct`, e o resto do projeto continua sem dependência nova.

# Tolerância de simplificação dos polígonos guardados, em graus (~55 m).
# O arquivo tem 19 MB e polígonos de até 76 mil vértices; para desenhar mapa e
# testar contenção, essa densidade é desperdício.
TOLERANCIA_BACIA_GRAUS = 0.0005


def _ler_dbf(caminho: str | Path) -> tuple[list[tuple], list[dict]]:
    """Tabela de atributos do shapefile."""
    d = Path(caminho).read_bytes()
    n_reg, tam_cabecalho, tam_registro = struct.unpack("<IHH", d[4:12])

    campos, p = [], 32
    while d[p] != 0x0D:
        nome = d[p:p + 11].split(b"\x00")[0].decode("latin-1").strip()
        campos.append((nome, chr(d[p + 11]), d[p + 16]))
        p += 32

    linhas = []
    for i in range(n_reg):
        deslocamento = tam_cabecalho + i * tam_registro + 1  # +1 pula a exclusão
        registro = {}
        for nome, tipo, tamanho in campos:
            bruto = d[deslocamento:deslocamento + tamanho].decode("utf-8", "replace").strip()
            if tipo in "NF":
                try:
                    bruto = float(bruto) if "." in bruto else int(bruto)
                except ValueError:
                    bruto = None
            registro[nome] = bruto
            deslocamento += tamanho
        linhas.append(registro)
    return campos, linhas


def _ler_shp_poligonos(caminho: str | Path) -> list[list[list[tuple]]]:
    """Anéis de cada polígono do .shp, em ordem de registro."""
    d = Path(caminho).read_bytes()
    p, formas = 100, []
    while p < len(d):
        _numero, tamanho = struct.unpack(">ii", d[p:p + 8])
        p += 8
        fim = p + tamanho * 2
        tipo = struct.unpack("<i", d[p:p + 4])[0]
        # 5 = Polygon, 15 = PolygonZ, 25 = PolygonM. O bloco XY é idêntico nos
        # três — Z e M vêm DEPOIS dos pontos, então basta ignorá-los. Este
        # arquivo é do tipo 15, e tratá-lo como 5 devolvia zero polígono.
        if tipo not in (5, 15, 25):
            p = fim
            continue
        n_partes, n_pontos = struct.unpack("<ii", d[p + 36:p + 44])
        q = p + 44
        partes = list(struct.unpack(f"<{n_partes}i", d[q:q + 4 * n_partes]))
        q += 4 * n_partes
        coords = struct.unpack(f"<{2 * n_pontos}d", d[q:q + 16 * n_pontos])
        aneis = []
        for k, inicio in enumerate(partes):
            final = partes[k + 1] if k + 1 < len(partes) else n_pontos
            aneis.append([(coords[2 * j], coords[2 * j + 1]) for j in range(inicio, final)])
        formas.append(aneis)
        p = fim
    return formas


def carregar_bacias_shapefile(caminho_shp: str | Path) -> list[dict]:
    """(nome, área declarada, polígono shapely) de cada bacia do arquivo."""
    from shapely.geometry import Polygon

    base = str(caminho_shp)[:-4] if str(caminho_shp).lower().endswith(".shp") else str(caminho_shp)
    _, registros = _ler_dbf(base + ".dbf")
    formas = _ler_shp_poligonos(base + ".shp")

    bacias = []
    for registro, aneis in zip(registros, formas):
        if not aneis:
            continue
        externo = max(aneis, key=len)
        buracos = [a for a in aneis if a is not externo and len(a) >= 4]
        try:
            # buffer(0) conserta auto-interseção, comum em contorno hidrográfico
            poligono = Polygon(externo, buracos).buffer(0)
        except Exception:
            continue
        if poligono.is_empty or not poligono.is_valid:
            continue
        bacias.append({
            "nome": str(registro.get("nome", "")).strip(),
            "area_km2": registro.get("area"),
            "poligono": poligono,
        })
    return bacias


def atribuir_bacias_oficiais(
    caminho_shp: str | Path, db_path: str = CAMINHO_BANCO_PADRAO
) -> dict:
    """Atribui a bacia oficial a cada estação e guarda os polígonos."""
    from shapely.geometry import Point, mapping
    from shapely.strtree import STRtree

    criar_schema_geo(db_path)
    bacias = carregar_bacias_shapefile(caminho_shp)
    if not bacias:
        return {"bacias": 0, "atribuidas": 0, "fora": 0}

    poligonos = [b["poligono"] for b in bacias]
    indice = STRtree(poligonos)

    with _conectar(db_path) as con:
        estacoes = pd.read_sql_query(
            "SELECT id, lat, lon FROM estacao WHERE lat IS NOT NULL AND lon IS NOT NULL",
            con,
        )

    linhas, fora = [], 0
    for r in estacoes.itertuples():
        ponto = Point(r.lon, r.lat)
        achou = None
        for i in indice.query(ponto):
            if poligonos[i].contains(ponto):
                achou = bacias[i]
                break
        if achou is None:
            fora += 1
            continue
        linhas.append({
            "id_estacao": r.id, "bacia": achou["nome"],
            "area_km2": achou["area_km2"], "atribuido_em": _agora(),
        })

    with _conectar(db_path) as con:
        if linhas:
            con.executemany(
                "INSERT OR REPLACE INTO bacia_oficial "
                "(id_estacao, bacia, area_km2, atribuido_em) "
                "VALUES (:id_estacao,:bacia,:area_km2,:atribuido_em)",
                linhas,
            )
        for b in bacias:
            simplificado = b["poligono"].simplify(
                TOLERANCIA_BACIA_GRAUS, preserve_topology=True
            )
            con.execute(
                "INSERT OR REPLACE INTO bacia_poligono "
                "(nome, area_km2, geojson, obtido_em) VALUES (?,?,?,?)",
                (b["nome"], b["area_km2"],
                 json.dumps(mapping(simplificado)), _agora()),
            )

    return {
        "bacias": len(bacias), "atribuidas": len(linhas), "fora": fora,
        "estacoes": len(estacoes),
    }


def bacia_oficial_da_estacao(
    estacao_id: str, db_path: str = CAMINHO_BANCO_PADRAO
) -> str | None:
    """Bacia oficial da estação, ou None se ainda não foi atribuída."""
    try:
        criar_schema_geo(db_path)
        with _conectar(db_path) as con:
            linha = con.execute(
                "SELECT bacia FROM bacia_oficial WHERE id_estacao = ?", (estacao_id,)
            ).fetchone()
    except Exception:
        return None
    return linha[0] if linha else None


# -----------------------------------------------------------------------------
# ÁREA DE DRENAGEM POR ESTAÇÃO
# -----------------------------------------------------------------------------
# O modelo agrupado usava a área da BACIA como preditor: 26.315 km² para toda
# estação do Taquari-Antas, de Santa Tereza a Estrela. Dentro de uma bacia a
# variável era constante, e não distinguia cabeceira de foz — justamente a
# distinção que ela deveria trazer.
#
# O inventário aberto do SNIRH publica `AreaDrenagem` por estação. Santa Tereza
# não drena os mesmos 26 mil km² que Estrela, e agora o modelo sabe disso.

SERVICO_ESTACOES_ANA = (
    "https://portal1.snirh.gov.br/server/rest/services/"
    "Esta%C3%A7%C3%B5es_Hidrometeorol%C3%B3gicas_SNIRH/FeatureServer/0/query"
)


def _inventario_area_ana(uf: str = "RIO GRANDE DO SUL") -> dict[str, float]:
    """`{codigo sem zeros à esquerda: área km²}` do inventário aberto da ANA."""
    import requests

    mapa: dict[str, float] = {}
    deslocamento = 0
    while True:
        # O serviço devolve página HTML de erro em vez de JSON de vez em
        # quando. Sem repetição, uma falha no meio da paginação derrubava a
        # coleta inteira e deixava metade das estações sem área.
        feicoes = None
        for tentativa in range(3):
            try:
                resposta = requests.get(
                    SERVICO_ESTACOES_ANA,
                    params={
                        "where": f"UF='{uf}' AND AreaDrenagem>0",
                        "outFields": "Codigo,AreaDrenagem",
                        "returnGeometry": "false", "f": "json",
                        "resultOffset": deslocamento, "resultRecordCount": 1000,
                    },
                    timeout=180, verify=False,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                feicoes = resposta.json().get("features", [])
                break
            except Exception:
                if tentativa == 2:
                    feicoes = None
                else:
                    time.sleep(2 * (tentativa + 1))

        if not feicoes:
            break
        for f in feicoes:
            a = f["attributes"]
            mapa[str(a["Codigo"]).lstrip("0")] = float(a["AreaDrenagem"])
        deslocamento += len(feicoes)
        if len(feicoes) < 1000:
            break
    return mapa


def _casar_codigo(codigo, mapa: dict[str, float]) -> tuple[float | None, str]:
    """Casa o código da estação com o do inventário da ANA.

    O mesmo posto aparece com 7 ou 8 dígitos conforme a fonte — Santa Tereza é
    `8647260` no SACE e `86472600` na ANA. Sem tolerar o zero final, 28 das 48
    estações que têm área ficariam de fora.
    """
    if codigo is None or (isinstance(codigo, float) and pd.isna(codigo)):
        return None, "sem código"
    c = str(codigo).strip().lstrip("0")
    if not c:
        return None, "sem código"
    if c in mapa:
        return mapa[c], "exato"
    if c + "0" in mapa:
        return mapa[c + "0"], "faltava um zero"
    if c.endswith("0") and c[:-1] in mapa:
        return mapa[c[:-1]], "zero a mais"
    return None, "sem par no inventário"


def atualizar_areas_drenagem(db_path: str = CAMINHO_BANCO_PADRAO) -> pd.DataFrame:
    """Busca a área de drenagem de cada estação e grava na tabela local."""
    criar_schema_geo(db_path)
    mapa = _inventario_area_ana()

    with _conectar(db_path) as con:
        estacoes = pd.read_sql_query("SELECT id, nome, codigo FROM estacao", con)

    linhas = []
    for r in estacoes.itertuples():
        area, como = _casar_codigo(r.codigo, mapa)
        linhas.append({
            "id_estacao": r.id, "codigo_ana": str(r.codigo),
            "area_km2": area, "casamento": como, "obtido_em": _agora(),
        })

    with _conectar(db_path) as con:
        con.executemany(
            "INSERT OR REPLACE INTO area_drenagem "
            "(id_estacao, codigo_ana, area_km2, casamento, obtido_em) "
            "VALUES (:id_estacao,:codigo_ana,:area_km2,:casamento,:obtido_em)",
            linhas,
        )
    return pd.DataFrame(linhas)


def area_da_estacao(estacao_id: str, db_path: str = CAMINHO_BANCO_PADRAO) -> float | None:
    """Área de drenagem da estação, ou None se a ANA não publica para ela.

    É o gancho que `georisk_hidrologia` usa: havendo área por estação, ela
    substitui a da bacia; não havendo, o comportamento antigo continua.
    """
    try:
        criar_schema_geo(db_path)
        with _conectar(db_path) as con:
            linha = con.execute(
                "SELECT area_km2 FROM area_drenagem WHERE id_estacao = ?",
                (estacao_id,),
            ).fetchone()
    except Exception:
        return None
    return float(linha[0]) if linha and linha[0] else None


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Camada geoespacial do GeoRisk-RS.")
    ap.add_argument("--banco", default=CAMINHO_BANCO_PADRAO)
    ap.add_argument("--inventario", action="store_true", help="listar manchas já baixadas")
    ap.add_argument("--cn", metavar="BACIA", help="calcular o CN de uma bacia")
    ap.add_argument("--bacias", metavar="SHP",
                    help="atribuir a bacia oficial de cada estacao pelo shapefile "
                         "das 26 bacias do RS")
    args = ap.parse_args()

    if args.bacias:
        r = atribuir_bacias_oficiais(args.bacias, args.banco)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        raise SystemExit(0)

    if args.inventario:
        for m in municipios_com_mancha(args.banco):
            print(m)
        for c in cns_calculados(args.banco):
            print(f"{c['nome']}: CN={c['cn_medio']} ({c['amostras']} amostras)")
    elif args.cn:
        print(json.dumps(
            calcular_cn_bacia(args.cn, args.banco,
                              progresso=lambda f, m: print(f"[{f:5.0%}] {m}"),
                              recalcular=True),
            indent=2, ensure_ascii=False))
    else:
        print(json.dumps(
            preparar_tudo(args.banco, progresso=lambda f, m: print(f"[{f:5.0%}] {m}")),
            indent=2, ensure_ascii=False))
