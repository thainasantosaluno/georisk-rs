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
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

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


def _conectar(db_path: str = CAMINHO_BANCO_PADRAO) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    return con


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
    ns = {"k": "http://www.opengis.net/kml/2.2"}

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
    avisar(0.88, f"{len(geo_solo)} polígonos de solo classificados.")

    # --- Amostragem
    from collections import Counter
    contagem_uso, contagem_grupo, contagem_ordem = Counter(), Counter(), Counter()
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
        grupo = None
        if arvore_solo is not None:
            for idx in arvore_solo.query(ponto):
                if geo_solo[idx].contains(ponto):
                    grupo = grupo_solo[idx]
                    contagem_ordem[ordem_solo[idx]] += 1
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

    cns = {}
    bacias = list(BACIAS_SACE)
    for i, bacia in enumerate(bacias):
        base = 0.55 + i * (0.44 / len(bacias))
        avisar(base, f"Calculando CN da bacia {bacia}…")
        try:
            r = calcular_cn_bacia(
                bacia, db_path,
                progresso=lambda f, m: avisar(base + f * (0.44 / len(bacias)), m),
            )
            cns[bacia] = r["cn_medio"] if r else None
        except Exception as exc:
            cns[bacia] = None
            avisar(base, f"CN da bacia {bacia} falhou: {type(exc).__name__}")

    avisar(1.0, "Camada geoespacial pronta.")
    return {"manchas_sgb_novas": n_sgb, "manchas_defesa_civil_novas": n_dc, "cn_por_bacia": cns}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Camada geoespacial do GeoRisk-RS.")
    ap.add_argument("--banco", default=CAMINHO_BANCO_PADRAO)
    ap.add_argument("--inventario", action="store_true", help="listar manchas já baixadas")
    ap.add_argument("--cn", metavar="BACIA", help="calcular o CN de uma bacia")
    args = ap.parse_args()

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
