"""
GeoRisk-RS — PUBLICAÇÃO
=======================
Gera os arquivos que um site público consome. Nada aqui fala com a internet
nem com a fonte: lê o banco já coletado e serializa.

POR QUE ESTÁTICO
----------------
O objetivo é consulta pública gratuita, e um serviço com banco hospedado custa
dinheiro todo mês e cai quando a conta acaba. Aqui a conta é outra: o coletor
já roda de 3 em 3 horas no GitHub Actions e já versiona o resultado. Publicar é
só escrever JSON junto — que o GitHub Pages serve de graça, com CDN, sem
servidor para manter e sem banco para hospedar.

O custo dessa escolha é que o site é tão fresco quanto a última rodada do
coletor. Por isso todo arquivo carrega `gerado_em` e `idade_minutos`, e a página
mostra a idade em vez de fingir tempo real.

O QUE É PUBLICADO
-----------------
    site/api/resumo.json      cabeçalho, frescor, limitações declaradas
    site/api/estacoes.json    as estações com leitura atual e cotas oficiais
    site/api/projecoes.json   quanto sobe, em quanto tempo, com que régua

O QUE NÃO É PUBLICADO
---------------------
A série de 15 min inteira (2 MB por rodada, e o repositório já a arquiva em
`dados/serie/`) e os campos internos de diagnóstico do modelo. O público
precisa do estado e da projeção, não do resíduo do ajuste.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import georisk_dados as gd
import georisk_hidrologia as gh

PASTA_PADRAO = "site"

# Limitações que acompanham TODA publicação. Ficam no JSON, não só na página,
# para que quem consumir a API por fora não as perca pelo caminho.
LIMITACOES = [
    "Não há previsão meteorológica: o sistema trabalha com a chuva que já caiu. "
    "Não substitui alerta do INMET, da Defesa Civil ou do CEMADEN.",
    "A projeção é útil até cerca de metade do tempo de resposta da bacia. "
    "Além disso o intervalo de incerteza cobre quase todo o resultado.",
    "Nem toda estação tem cota oficial de atenção/alerta/inundação. Só o SACE "
    "publica esses limiares; onde não há, nenhum valor é arbitrado.",
    "Mancha de inundação modelada existe para poucos municípios. Onde não há, "
    "a extensão mostrada é estimativa, não modelagem hidráulica.",
]

FONTES = [
    {"nome": "SACE / SGB-CPRM", "papel": "nível, cotas oficiais e série de 15 min"},
    {"nome": "ANA / Telemetria", "papel": "estações automáticas e área de drenagem"},
    {"nome": "IBGE / BDIA", "papel": "pedologia, uso da terra, geologia e drenagem"},
    {"nome": "Copernicus DEM GLO-30", "papel": "relevo"},
]

# Campos que vão ao público. O banco tem mais; o resto é diagnóstico.
CAMPOS_ESTACAO = [
    "id", "nome", "municipio", "rio", "bacia", "fonte", "tipo",
    "lat", "lon", "nivel_cm", "chuva_24h", "chuva_72h",
    "cota_atencao_cm", "cota_alerta_cm", "cota_inundacao_cm",
    "situacao", "cor", "medido_em",
]

CAMPOS_PROJECAO = [
    "id_estacao", "nome", "municipio", "bacia",
    "chuva_24h_mm", "chuva_72h_mm", "volume_escoado_m3", "cn_base",
    "cota_atual_cm", "pico_projetado_cm", "variacao_cm",
    "tc_horas", "instante_pico", "horizonte_util_horas",
    "confiavel", "origem_projecao", "tendencia", "classe", "motivo",
    "horas_ate_inundacao",
]


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _idade_minutos(instante: str | None) -> float | None:
    if not instante:
        return None
    try:
        return round(
            (pd.Timestamp.now() - pd.Timestamp(instante)).total_seconds() / 60, 1
        )
    except Exception:
        return None


def _limpar(df: pd.DataFrame, campos: list[str]) -> list[dict]:
    """DataFrame -> lista de dicionários, com NaN virando null de verdade.

    `df.to_json` já faz isso, mas passar por `json.dumps` mantém um só caminho
    de serialização e evita que a formatação de float mude entre arquivos.
    """
    presentes = [c for c in campos if c in df.columns]
    return json.loads(df[presentes].to_json(orient="records", date_format="iso"))


def _gravar(caminho: Path, conteudo: dict) -> Path:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps(conteudo, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return caminho


def publicar(pasta: str | Path = PASTA_PADRAO) -> dict:
    """Escreve os JSON públicos. Devolve um resumo do que foi gerado."""
    base = Path(pasta) / "api"
    agora = _agora()

    estacoes = gd.carregar_estacoes().drop(columns=["medido_em_dt"], errors="ignore")
    coleta = gd.ultima_coleta() or {}

    try:
        projecoes = gh.carregar_projecoes()
    except Exception:
        projecoes = pd.DataFrame()

    # --- resumo: o cabeçalho e, junto dele, o que o sistema NÃO faz
    fluvio = estacoes[estacoes["tipo"] == "FLUVIOMETRICA"]
    com_cota = fluvio["cota_atencao_cm"].notna().sum()
    calculado_em = (
        projecoes["calculado_em"].max() if not projecoes.empty else None
    )

    resumo = {
        "gerado_em": agora,
        "abrangencia": "Rio Grande do Sul",
        "coleta": {
            **coleta,
            "idade_minutos": _idade_minutos(coleta.get("terminada_em")),
        },
        "projecao": {
            "calculada_em": calculado_em,
            "idade_minutos": _idade_minutos(calculado_em),
            "estacoes": int(len(projecoes)),
        },
        "contagem": {
            "estacoes": int(len(estacoes)),
            "fluviometricas": int(len(fluvio)),
            "com_cota_oficial": int(com_cota),
            "sem_cota_oficial": int(len(fluvio) - com_cota),
            "em_inundacao": int((estacoes["cor"] == "red").sum()),
            "em_alerta": int((estacoes["cor"] == "orange").sum()),
            "em_atencao": int((estacoes["cor"] == "gold").sum()),
        },
        "fontes": FONTES,
        "limitacoes": LIMITACOES,
        "licenca": "Dados públicos das fontes citadas. Uso livre, sem garantia.",
    }

    escritos = [
        _gravar(base / "resumo.json", resumo),
        _gravar(base / "estacoes.json", {
            "gerado_em": agora,
            "total": int(len(estacoes)),
            "estacoes": _limpar(estacoes, CAMPOS_ESTACAO),
        }),
        _gravar(base / "projecoes.json", {
            "gerado_em": agora,
            "calculada_em": calculado_em,
            "total": int(len(projecoes)),
            "legenda_classes": {
                "inundacao": "pico projetado atinge a cota de inundação",
                "alerta": "pico projetado atinge a cota de alerta",
                "atencao": "pico projetado atinge a cota de atenção",
                "abaixo": "pico abaixo de todas as cotas oficiais",
                "subida_forte": "sem cota oficial — subida grande para o que o rio costuma fazer",
                "subida_moderada": "sem cota oficial — subida moderada",
                "subida_leve": "sem cota oficial — subida pequena",
                "estavel": "sem cota oficial — praticamente sem variação",
                "indefinida": "sem projeção confiável no momento",
            },
            "projecoes": _limpar(projecoes, CAMPOS_PROJECAO) if not projecoes.empty else [],
        }),
    ]

    return {
        "arquivos": [str(c) for c in escritos],
        "bytes": {c.name: c.stat().st_size for c in escritos},
        "estacoes": int(len(estacoes)),
        "projecoes": int(len(projecoes)),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Publica os JSON do site público.")
    ap.add_argument("--pasta", default=PASTA_PADRAO)
    args = ap.parse_args()

    r = publicar(args.pasta)
    print(json.dumps(r, ensure_ascii=False, indent=2))
