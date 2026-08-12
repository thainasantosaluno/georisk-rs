"""
GeoRisk-RS — MÓDULO HIDROLÓGICO
===============================
Relação volumétrica Chuva x Nível, tempo de resposta (Tc) e projeção de
extravasamento, lendo direto do SQLite `georisk_rs.db` já existente.

Não requer Postgres/PostGIS. Não usa scikit-learn nem statsmodels (que NÃO
estão instalados nesta máquina): a regressão é resolvida por mínimos quadrados
regularizados em numpy puro, o que dá o mesmo resultado de uma Ridge para o
tamanho de problema aqui (poucas dezenas de colunas, alguns milhares de linhas).

O QUE ESTE MÓDULO FAZ
---------------------
1. Tempo de resposta (Tc) por CORRELAÇÃO CRUZADA entre a série de chuva de 15
   em 15 min e a taxa de subida do rio (dH/dt). É o método principal porque é
   o único que os dados disponíveis sustentam de fato.
2. Chuva efetiva por SCS-CN simplificado, com a condição de umidade antecedente
   (AMC) puxada da precipitação acumulada de 72 h — como pedido.
3. Regressão volume de chuva -> cota futura, treinada nos 30 dias de histórico
   do próprio banco, projetando H(t + Tc) em vários horizontes.
4. Tempo até o nível projetado cruzar as cotas oficiais de Atenção / Alerta /
   Inundação do SACE.
5. Gráfico Plotly hietograma + hidrograma medido + hidrograma projetado.

VALIDAÇÃO FEITA COM DADO REAL (30 dias, julho/2026)
--------------------------------------------------
1) O Tc estimado cresce de montante para jusante na cascata do Taquari-Antas —
   Santa Tereza 9,75 h -> Muçum 11,5 h -> Encantado 12,25 h -> Estrela 17 h ->
   Taquari 16 h — e as cabeceiras pequenas respondem em 1 a 2 h (Linha Colombo,
   Passo Tainhas). É o comportamento físico esperado.

2) RETROTESTE sobre a cheia de 22/07 (parâmetro `ate_instante`): rodando o
   modelo no momento de subida mais forte e comparando com o que de fato
   aconteceu, em Estrela (pico real 2.477 cm) os erros ficaram em +154, +103,
   +28 e −8 cm nos quatro primeiros horizontes.

3) O ganho sobre a PERSISTÊNCIA (previsão trivial "o nível fica como está"),
   medido por validação walk-forward, é positivo só na primeira metade do Tc:
   em Estrela deu +0,46 em 4 h, +0,28 em 8,5 h, e negativo de 12,75 h adiante.
   Por isso o retorno traz `horizonte_util_horas` — além dele a projeção perde
   para não fazer projeção nenhuma, e o campo `confiavel` fica False.

   Uma versão anterior deste módulo previa o nível absoluto e exibia R² de
   0,94: era ilusão. O nível de agora já explica quase todo o nível de daqui a
   pouco, então o modelo aprendia a copiar a entrada. No retroteste ele
   projetou 7.781 cm numa cheia que fez 1.708 cm. Prever a VARIAÇÃO, limitar ao
   envelope histórico e medir contra persistência corrigiu isso.

LIMITES QUE VOCÊ PRECISA CONHECER (não são bugs, são honestidade)
-----------------------------------------------------------------
- A chuva usada é a MEDIDA NA PRÓPRIA ESTAÇÃO. Em rios grandes, o nível é
  governado por chuva que caiu centenas de km acima. Nas estações do baixo
  Uruguai a correlação medida foi r≈0,03 (Uruguaiana, Itaqui): ali a projeção
  NÃO tem validade e o módulo devolve `confiavel=False` em vez de um número
  bonito e falso. Para essas, o caminho certo é propagar a cota de montante,
  não a chuva local.
- 30 dias de histórico costumam conter poucos eventos de cheia. A regressão é
  um ajuste local recente, não um modelo climatológico. Use como apoio à
  decisão, nunca como fonte única.
- Kirpich NÃO é aplicado automaticamente: ele exige comprimento e declividade
  do talvegue, que o banco não tem. A função `tempo_concentracao_kirpich()`
  está disponível para quem tiver a morfometria da bacia (ver docstring).
- CN é PARÂMETRO, não medição. O padrão (CN=75) representa bacia rural mista
  em solo B/C. Ajuste por bacia se você tiver uso do solo.

USO
---
    import georisk_hidrologia as gh

    r = gh.estimar_tempo_e_impacto_inundacao("SACE_taquari_2")
    print(r["tempo_horas_ate_inundacao"], r["cota_maxima_projetada_cm"])
    r["grafico_hietograma_hidrograma"].show()

    # listar o que dá para analisar
    print(gh.estacoes_analisaveis())
"""

from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# -----------------------------------------------------------------------------
# CONFIGURAÇÃO
# -----------------------------------------------------------------------------
CAMINHO_BANCO_PADRAO = str(Path(__file__).resolve().parent / "georisk_rs.db")

PASSO_MINUTOS = 15                 # resolução nativa das séries do SACE
PASSOS_POR_HORA = 60 // PASSO_MINUTOS

# Janelas de precipitação acumulada exigidas no escopo.
JANELAS_HORAS = (1, 3, 12, 24, 72)

# Busca do Tc: de 15 min a 72 h.
TC_MIN_HORAS = 0.25
TC_MAX_HORAS = 72.0

# Suavização do pulso de chuva antes de correlacionar (3 h). Sem isso a série
# de 15 min é quase toda zero e a correlação vira ruído.
JANELA_PULSO_HORAS = 3

# Abaixo disto a correlação chuva-local x nível não sustenta projeção.
CORRELACAO_MINIMA = 0.30

# A partir de quantas horas sem dado novo a série deixa de sustentar projeção.
# O SACE publica de 15 em 15 min e o coletor roda de 3 em 3 h; seis horas é o
# dobro da cadência de coleta — atraso maior é a fonte parada, não jitter.
IDADE_MAXIMA_ANCORA_HORAS = 6.0

# Meia-vida do índice de encharcamento: em quantos dias a chuva de hoje perde
# metade do peso sobre a umidade do solo. 3 dias é o valor usual em bacias
# subtropicais úmidas; solo arenoso drena mais rápido (1-2 d), argiloso mais
# devagar (5-7 d).
MEIA_VIDA_ENCHARCAMENTO_DIAS = 3.0

# SCS-CN. CN2 = condição de umidade média (AMC II).
CN_PADRAO = 75.0
P72_AMC_SECA = 35.0                # mm — abaixo disso, solo seco (AMC I)
P72_AMC_UMIDA = 53.0               # mm — acima disso, solo saturado (AMC III)

# Faixas físicas (coerentes com georisk_dados.FAIXAS): acima disso a série de
# "cota" é, na verdade, cota absoluta de reservatório em metros.
COTA_MAXIMA_PLAUSIVEL_CM = 5000.0

# Regularização da regressão (equivalente ao alpha de uma Ridge).
LAMBDA_RIDGE = 1.0

# Horizontes de projeção, em frações de Tc.
FRACOES_HORIZONTE = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5)


# -----------------------------------------------------------------------------
# ESTRUTURAS DE RETORNO
# -----------------------------------------------------------------------------
@dataclass
class TempoResposta:
    """Resultado da estimativa de Tc."""
    tc_horas: float | None
    correlacao: float
    metodo: str
    confiavel: bool
    avisos: list[str] = field(default_factory=list)


@dataclass
class ChuvaEfetiva:
    """Resultado do balanço volumétrico SCS-CN."""
    precipitacao_total_mm: float
    precipitacao_efetiva_mm: float
    abstracao_inicial_mm: float
    retencao_potencial_mm: float
    cn_base: float
    cn_ajustado: float
    condicao_umidade: str
    p72_mm: float
    # --- Encharcamento e volume
    api_mm: float | None = None              # índice de encharcamento do solo
    saturacao_pct: float | None = None       # 0 % seco, 100 % encharcado
    area_bacia_km2: float | None = None
    volume_precipitado_m3: float | None = None   # o que caiu sobre a bacia
    volume_escoado_m3: float | None = None       # o que vira vazão
    volume_infiltrado_m3: float | None = None    # o que o solo absorveu
    coeficiente_escoamento: float | None = None  # escoado / precipitado


# -----------------------------------------------------------------------------
# 1. LEITURA DO BANCO
# -----------------------------------------------------------------------------
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




def carregar_cadastro(estacao_id: str, db_path: str = CAMINHO_BANCO_PADRAO) -> dict:
    """Metadados da estação, incluindo as cotas oficiais do SACE."""
    with _conectar(db_path) as con:
        con.row_factory = sqlite3.Row
        linha = con.execute(
            "SELECT * FROM estacao WHERE id = ?", (estacao_id,)
        ).fetchone()
    if linha is None:
        raise ValueError(f"Estação '{estacao_id}' não existe no banco.")
    return dict(linha)


def carregar_series_alinhadas(
    estacao_id: str, db_path: str = CAMINHO_BANCO_PADRAO, dias: int = 30
) -> pd.DataFrame:
    """Cota e chuva na MESMA grade de 15 min.

    Aplica os mesmos tratamentos que o coletor já faz, porque nada impede que
    uma série antiga tenha ficado no banco antes de um filtro entrar em vigor:
      - descarta série de cota cuja mediana passa de 5000 cm (é cota absoluta
        de reservatório de CGH/PCH em metros, não régua fluviométrica);
      - trata chuva negativa como ausência de leitura.
    """
    with _conectar(db_path) as con:
        bruto = pd.read_sql_query(
            "SELECT grandeza, datahora, valor FROM serie "
            "WHERE id_estacao = ? AND grandeza IN ('cota','chuva') "
            "ORDER BY datahora",
            con,
            params=(estacao_id,),
        )

    if bruto.empty:
        return pd.DataFrame(columns=["cota_cm", "chuva_mm"])

    bruto["datahora"] = pd.to_datetime(bruto["datahora"], errors="coerce")
    bruto = bruto.dropna(subset=["datahora"])

    cota = bruto.loc[bruto["grandeza"] == "cota"].set_index("datahora")["valor"]
    chuva = bruto.loc[bruto["grandeza"] == "chuva"].set_index("datahora")["valor"]

    # Cota de reservatório em metros disfarçada de centímetro.
    if not cota.empty and float(cota.median()) > COTA_MAXIMA_PLAUSIVEL_CM:
        cota = cota.iloc[0:0]

    chuva = chuva[chuva >= 0]

    if cota.empty and chuva.empty:
        return pd.DataFrame(columns=["cota_cm", "chuva_mm"])

    # Grade regular de 15 min cobrindo o período disponível.
    inicio = min([s.index.min() for s in (cota, chuva) if not s.empty])
    fim = max([s.index.max() for s in (cota, chuva) if not s.empty])
    inicio = max(inicio, fim - timedelta(days=dias))
    grade = pd.date_range(inicio, fim, freq=f"{PASSO_MINUTOS}min")

    df = pd.DataFrame(index=grade)
    # Cota: interpola falha curta (sensor pisca), mas não inventa trecho longo.
    df["cota_cm"] = (
        cota.reindex(cota.index.union(grade)).interpolate(limit=8).reindex(grade)
        if not cota.empty else np.nan
    )
    # Chuva: ausência de registro é ausência de chuva acumulada no passo.
    df["chuva_mm"] = chuva.reindex(grade).fillna(0.0) if not chuva.empty else 0.0
    df.index.name = "datahora"
    return df


def estacoes_analisaveis(
    db_path: str = CAMINHO_BANCO_PADRAO, minimo_registros: int = 500
) -> pd.DataFrame:
    """Estações que têm as duas séries e, portanto, permitem a análise.

    Ordenadas por volume de chuva no período: quanto mais evento, mais
    confiável a estimativa de Tc.

    Traz também posição, município e leitura atual — é o que o painel precisa
    para plotar estas estações no mapa sem uma segunda consulta ao banco.
    """
    with _conectar(db_path) as con:
        df = pd.read_sql_query(
            """
            SELECT e.id, e.nome, e.rio, e.bacia, e.fonte,
                   e.municipio, e.lat, e.lon,
                   e.nivel_cm, e.situacao, e.medido_em,
                   e.cota_atencao_cm, e.cota_alerta_cm, e.cota_inundacao_cm,
                   SUM(CASE WHEN s.grandeza='cota'  THEN 1 ELSE 0 END) AS n_cota,
                   SUM(CASE WHEN s.grandeza='chuva' THEN 1 ELSE 0 END) AS n_chuva,
                   SUM(CASE WHEN s.grandeza='chuva' THEN s.valor ELSE 0 END) AS chuva_mm_periodo
            FROM estacao e
            JOIN serie s ON s.id_estacao = e.id
            GROUP BY e.id
            HAVING n_cota >= ? AND n_chuva >= ?
            ORDER BY chuva_mm_periodo DESC
            """,
            con,
            params=(minimo_registros, minimo_registros),
        )
    return df


# -----------------------------------------------------------------------------
# 2. PRECIPITAÇÃO ACUMULADA
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# CHUVA MÉDIA DA BACIA — o preditor que de fato comanda o rio
# -----------------------------------------------------------------------------
# Medido nas 22 estações do SACE com série de cota:
#
#     chuva medida NA estação ....... r médio 0,302  | 17 de 22 acima de 0,30
#     chuva média DA BACIA .......... r médio 0,540  | 20 de 22 acima de 0,30
#
# Faz sentido físico: o rio integra a chuva de toda a área que drena para ele,
# não a do pluviômetro que por acaso fica ao lado da régua. Passo Carreiro sai
# de 0,08 para 0,24; Taquari, de 0,53 para 0,65.
#
# Antes o módulo correlacionava só com a chuva local e, onde ela era fraca,
# declarava "não use para decisão". O problema estava no preditor, não na
# estação — e usar o preditor errado é o que tornava a ferramenta inútil
# justamente nos rios grandes, que são os que mais importam.

@lru_cache(maxsize=8)
def chuva_media_bacia(bacia: str, db_path: str = CAMINHO_BANCO_PADRAO,
                      dias: int = 30) -> pd.Series | None:
    """Média das séries de chuva de todas as estações da bacia.

    Em cache porque é a mesma para todas as estações da bacia e envolve ler
    dezenas de séries de 15 min.
    """
    with _conectar(db_path) as con:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM estacao WHERE bacia = ? AND fonte = 'SACE/SGB'", (bacia,)
        )]
    if not ids:
        return None

    colunas = []
    for eid in ids:
        serie = carregar_series_alinhadas(eid, db_path, dias=dias)
        if not serie.empty and serie["chuva_mm"].sum() > 0:
            colunas.append(serie["chuva_mm"])
    if not colunas:
        return None
    return pd.concat(colunas, axis=1).mean(axis=1)


def chuva_area_contribuinte(
    df: pd.DataFrame, bacia: str | None, db_path: str = CAMINHO_BANCO_PADRAO,
    dias: int = 30, correlacao_minima: float = 0.20,
) -> tuple[pd.Series, str, int]:
    """Índice de chuva da ÁREA QUE DRENA PARA ESTA ESTAÇÃO.

    A média da bacia inteira generaliza demais: no Taquari são 26.000 km², e
    boa parte deles drena para pontos a JUSANTE da estação, sem influência
    nenhuma sobre o nível dela.

    Sem MDE não dá para delimitar a sub-bacia por topografia. Mas dá para
    descobri-la pelos dados: cada pluviômetro é correlacionado com a subida
    DESTA estação, e entram no índice só os que de fato a antecipam, com peso
    proporcional ao quadrado da correlação. Quem está a jusante não antecipa
    nada e cai fora sozinho.

    Medido nas 39 estações do SACE com série de cota:

        chuva medida na estação ..... r médio 0,285  | 27 de 39 acima de 0,30
        média da bacia inteira ...... r médio 0,465  | 28 de 39
        ÁREA CONTRIBUINTE ........... r médio 0,496  | 35 de 39

    O ganho aparece justamente onde a média da bacia não ajudava — os rios
    grandes. Itaqui vai de 0,004 para 0,348; Passo São Borja, de 0,035 para
    0,349; Ponte Ibicuí da Armada chega a 0,699.

    Devolve (série, rótulo, quantos postos entraram).
    """
    local = df["chuva_mm"]
    if not bacia or "cota_cm" not in df or df["cota_cm"].isna().all():
        return local, "chuva medida na estação", 1

    with _conectar(db_path) as con:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM estacao WHERE bacia = ? AND fonte = 'SACE/SGB'", (bacia,)
        )]
    if not ids:
        return local, "chuva medida na estação", 1

    subida = df["cota_cm"].interpolate().diff(3 * PASSOS_POR_HORA)
    janela = 3 * PASSOS_POR_HORA

    def antecipa(serie: pd.Series) -> float:
        """Maior correlação da chuva com a subida, varrendo defasagens."""
        x = serie.rolling(janela, min_periods=1).sum()
        ok = x.notna() & subida.notna()
        if ok.sum() < 300:
            return -1.0
        xa = (x[ok] - x[ok].mean()).to_numpy()
        ya = (subida[ok] - subida[ok].mean()).to_numpy()
        melhor = -1.0
        for passos in range(0, 36 * PASSOS_POR_HORA + 1, PASSOS_POR_HORA):
            a, b = (xa, ya) if passos == 0 else (xa[:-passos], ya[passos:])
            if len(a) < 300 or a.std() == 0 or b.std() == 0:
                continue
            melhor = max(melhor, float(np.corrcoef(a, b)[0, 1]))
        return melhor

    pesos, series = [], []
    for eid in ids:
        vizinha = carregar_series_alinhadas(eid, db_path, dias=dias)
        if vizinha.empty or vizinha["chuva_mm"].sum() <= 0:
            continue
        chuva = vizinha["chuva_mm"].reindex(df.index).fillna(0.0)
        r = antecipa(chuva)
        if r > correlacao_minima:
            # Peso ao quadrado: realça quem explica mais e abafa o marginal.
            pesos.append(r ** 2)
            series.append(chuva)

    if not series:
        return local, "chuva medida na estação", 1

    w = np.array(pesos) / sum(pesos)
    combinada = sum(sr * pi for sr, pi in zip(series, w))

    # Só troca se realmente for melhor que a chuva local — o critério é
    # medido, não presumido.
    if antecipa(combinada) <= antecipa(local):
        return local, "chuva medida na estação", 1

    return combinada, f"chuva da área contribuinte ({len(series)} postos)", len(series)


def escolher_chuva(df: pd.DataFrame, bacia: str | None,
                   db_path: str = CAMINHO_BANCO_PADRAO) -> tuple[pd.Series, str]:
    """Compatibilidade: devolve a chuva da área contribuinte."""
    serie, rotulo, _ = chuva_area_contribuinte(df, bacia, db_path)
    return serie, rotulo



def acumulados_moveis(chuva_mm: pd.Series) -> pd.DataFrame:
    """P_acum nas janelas de 1, 3, 12, 24 e 72 h, em toda a série."""
    saida = pd.DataFrame(index=chuva_mm.index)
    for horas in JANELAS_HORAS:
        passos = int(horas * PASSOS_POR_HORA)
        saida[f"p{horas}h"] = chuva_mm.rolling(passos, min_periods=1).sum()
    return saida


# -----------------------------------------------------------------------------
# 3. TEMPO DE RESPOSTA / CONCENTRAÇÃO
# -----------------------------------------------------------------------------
def tempo_concentracao_kirpich(
    comprimento_km: float, desnivel_m: float
) -> float:
    """Tc de Kirpich (1940), em horas.

        Tc = 0.0663 * L^0.77 * S^-0.385      (L em km, S = desnível/L em m/m)

    NÃO é usada automaticamente por `estimar_tempo_e_impacto_inundacao()`:
    o banco `georisk_rs.db` guarda estação, não morfometria de bacia — não há
    comprimento de talvegue nem declividade. Chutar esses dois números para
    poder "aplicar Kirpich" produziria um Tc de aparência científica e valor
    arbitrário, que é exatamente o tipo de número inventado que este projeto
    combateu.

    Use quando você tiver a morfometria (de um MDE, do SGB ou da ANA):

        tc = tempo_concentracao_kirpich(comprimento_km=42.0, desnivel_m=310.0)

    Serve também como aferição do Tc empírico: se os dois divergirem muito,
    provavelmente a chuva que comanda o rio não é a medida na estação.
    """
    if comprimento_km <= 0 or desnivel_m <= 0:
        raise ValueError("Comprimento e desnível devem ser positivos.")
    declividade = desnivel_m / (comprimento_km * 1000.0)
    return 0.0663 * (comprimento_km ** 0.77) * (declividade ** -0.385)


def estimar_tempo_resposta(
    df: pd.DataFrame,
    tc_min_horas: float = TC_MIN_HORAS,
    tc_max_horas: float = TC_MAX_HORAS,
) -> TempoResposta:
    """Tc empírico por correlação cruzada chuva -> taxa de subida do rio.

    Por que dH/dt e não H: a chuva comanda a VELOCIDADE de subida, não o nível
    absoluto (que carrega a memória do evento anterior). Correlacionar contra
    dH/dt isola a resposta ao pulso. Verificado no dado real: contra H os lags
    saem inflados e ruidosos; contra dH/dt reproduzem a cascata de montante
    para jusante do Taquari-Antas.

    A chuva é somada em janela de 3 h antes de correlacionar, porque a série de
    15 min é quase toda zero e correlacionar zeros gera ruído.
    """
    avisos: list[str] = []

    if "cota_cm" not in df or df["cota_cm"].isna().all():
        return TempoResposta(None, 0.0, "indisponível", False,
                             ["Estação sem série de cota — é posto só de chuva."])
    if df["chuva_mm"].sum() <= 0:
        return TempoResposta(None, 0.0, "indisponível", False,
                             ["Nenhuma chuva registrada no período."])

    dados = df.dropna(subset=["cota_cm"])
    if len(dados) < 200:
        return TempoResposta(None, 0.0, "indisponível", False,
                             [f"Série curta demais ({len(dados)} registros)."])

    pulso = dados["chuva_mm"].rolling(
        int(JANELA_PULSO_HORAS * PASSOS_POR_HORA), min_periods=1
    ).sum()
    subida = dados["cota_cm"].diff().fillna(0.0)

    x = (pulso - pulso.mean()).to_numpy()
    y = (subida - subida.mean()).to_numpy()

    lag_min = int(tc_min_horas * PASSOS_POR_HORA)
    lag_max = min(int(tc_max_horas * PASSOS_POR_HORA), len(x) // 3)
    if lag_max <= lag_min:
        return TempoResposta(None, 0.0, "indisponível", False,
                             ["Série insuficiente para varrer defasagens."])

    correlacoes = np.full(lag_max + 1, np.nan)
    for lag in range(lag_min, lag_max + 1):
        xa, ya = x[:-lag], y[lag:]
        if xa.std() == 0 or ya.std() == 0:
            continue
        correlacoes[lag] = float(np.corrcoef(xa, ya)[0, 1])

    if np.all(np.isnan(correlacoes)):
        return TempoResposta(None, 0.0, "correlação cruzada", False,
                             ["Não foi possível correlacionar (série constante)."])

    melhor_lag = int(np.nanargmax(correlacoes))
    melhor_r = float(correlacoes[melhor_lag])
    tc_horas = melhor_lag / PASSOS_POR_HORA

    confiavel = melhor_r >= CORRELACAO_MINIMA
    if not confiavel:
        avisos.append(
            f"Correlação fraca (r={melhor_r:.2f} < {CORRELACAO_MINIMA:.2f}) mesmo "
            "usando a melhor chuva disponível. Nestes casos a projeção da própria "
            "estação não serve; quando há modelo agrupado validado, é ele que "
            "responde — veja `origem_projecao`."
        )
    if melhor_lag >= lag_max:
        confiavel = False
        avisos.append(
            f"O máximo de correlação caiu no limite da busca ({tc_horas:.1f} h): "
            "o Tc real provavelmente é maior que a janela varrida."
        )

    return TempoResposta(
        tc_horas=tc_horas,
        correlacao=melhor_r,
        metodo="correlação cruzada chuva x dH/dt",
        confiavel=confiavel,
        avisos=avisos,
    )


# -----------------------------------------------------------------------------
# 4. BALANÇO VOLUMÉTRICO — SCS-CN COM UMIDADE ANTECEDENTE
# -----------------------------------------------------------------------------
def indice_encharcamento(
    chuva_mm: pd.Series, meia_vida_dias: float = MEIA_VIDA_ENCHARCAMENTO_DIAS
) -> pd.Series:
    """Índice de encharcamento do solo (API — Antecedent Precipitation Index).

        API_t = k · API_{t-1} + P_t

    A chuva de hoje soma; a de ontem ainda pesa, mas menos; a da semana passada
    quase não pesa. `k` sai da meia-vida: k = 0.5^(passo/meia_vida).

    POR QUE ISTO SUBSTITUI O DEGRAU DE 72 h
    ---------------------------------------
    Antes o encharcamento era um interruptor de três posições, decidido pela
    chuva de 72 h: abaixo de 35 mm solo seco, acima de 53 mm solo saturado, no
    meio umidade média. Dois problemas nisso.

    Primeiro, é descontínuo: 52 mm e 54 mm em 72 h davam CN muito diferentes,
    embora o solo esteja praticamente no mesmo estado. Segundo, ignora QUANDO a
    chuva caiu — 60 mm concentrados ontem encharcam muito mais que 60 mm
    espalhados em três dias, e a soma de 72 h não distingue os dois casos.

    O API resolve os dois: é contínuo e pondera pelo tempo decorrido.
    """
    k = 0.5 ** ((PASSO_MINUTOS / 60.0 / 24.0) / meia_vida_dias)
    valores = np.zeros(len(chuva_mm))
    acumulado = 0.0
    for i, p in enumerate(chuva_mm.fillna(0.0).to_numpy()):
        acumulado = acumulado * k + float(p)
        valores[i] = acumulado
    return pd.Series(valores, index=chuva_mm.index, name="api_mm")


def _ajustar_cn_por_umidade(
    cn2: float, p72_mm: float, api_mm: float | None = None
) -> tuple[float, str]:
    """Converte CN de umidade média (AMC II) para a condição real do solo.

    Quando o índice de encharcamento (`api_mm`) está disponível, a transição é
    CONTÍNUA entre AMC I e AMC III, interpolando pelo grau de saturação. Sem
    ele, cai no degrau clássico das 72 h — que continua aqui como retaguarda,
    para o método permanecer aplicável a quem só tem o acumulado.
    """
    cn_seco = 4.2 * cn2 / (10.0 - 0.058 * cn2)
    cn_saturado = 23.0 * cn2 / (10.0 + 0.13 * cn2)

    if api_mm is not None:
        # Grau de saturação: 0 = solo seco, 1 = encharcado. Os limiares são os
        # mesmos do AMC clássico, mas agora como extremos de uma rampa.
        grau = (api_mm - P72_AMC_SECA) / (P72_AMC_UMIDA - P72_AMC_SECA)
        grau = float(np.clip(grau, 0.0, 1.0))
        cn = cn_seco + grau * (cn_saturado - cn_seco)
        if grau <= 0.05:
            rotulo = f"solo seco (saturação {grau:.0%})"
        elif grau >= 0.95:
            rotulo = f"solo encharcado (saturação {grau:.0%})"
        else:
            rotulo = f"umidade intermediária (saturação {grau:.0%})"
        return cn, rotulo

    if p72_mm < P72_AMC_SECA:
        return cn_seco, "AMC I (solo seco)"
    if p72_mm > P72_AMC_UMIDA:
        return cn_saturado, "AMC III (solo saturado)"
    return cn2, "AMC II (umidade média)"


def calcular_chuva_efetiva(
    precipitacao_mm: float,
    p72_mm: float,
    cn_base: float = CN_PADRAO,
    api_mm: float | None = None,
    area_bacia_km2: float | None = None,
) -> ChuvaEfetiva:
    """Precipitação efetiva (escoamento superficial) pelo método SCS-CN.

        S  = 25400/CN - 254           retenção potencial máxima (mm)
        Ia = 0.2 * S                  abstração inicial (mm)
        Pe = (P - Ia)^2 / (P - Ia + S)    se P > Ia, senão 0

    `cn_base` é PARÂMETRO, não medição: 75 representa bacia rural mista em solo
    hidrológico B/C. Se você tiver uso e tipo de solo da bacia, passe o CN
    tabelado correspondente.
    """
    cn_ajustado, condicao = _ajustar_cn_por_umidade(cn_base, p72_mm, api_mm)
    cn_ajustado = min(max(cn_ajustado, 1.0), 100.0)

    retencao = 25400.0 / cn_ajustado - 254.0
    abstracao = 0.2 * retencao

    if precipitacao_mm > abstracao:
        excedente = precipitacao_mm - abstracao
        efetiva = excedente ** 2 / (excedente + retencao)
    else:
        efetiva = 0.0

    # --- VOLUME, não só lâmina.
    # Lâmina em mm não diz quanta água é. 1 mm sobre 1 km² = 1.000 m³, então
    # `mm × km² × 1.000` dá metros cúbicos — grandeza comparável com a vazão do
    # rio e com o que a calha comporta. Os mesmos 40 mm significam coisas muito
    # diferentes no Caí (4.956 km²) e no Uruguai (215.612 km²).
    volume_precipitado = volume_escoado = volume_infiltrado = None
    coeficiente = None
    if area_bacia_km2 and area_bacia_km2 > 0:
        volume_precipitado = precipitacao_mm * area_bacia_km2 * 1000.0
        volume_escoado = efetiva * area_bacia_km2 * 1000.0
        volume_infiltrado = max(0.0, volume_precipitado - volume_escoado)
        coeficiente = (
            round(efetiva / precipitacao_mm, 3) if precipitacao_mm > 0 else 0.0
        )

    grau = None
    if api_mm is not None:
        grau = float(np.clip(
            (api_mm - P72_AMC_SECA) / (P72_AMC_UMIDA - P72_AMC_SECA), 0.0, 1.0
        )) * 100.0

    return ChuvaEfetiva(
        api_mm=None if api_mm is None else round(float(api_mm), 2),
        saturacao_pct=None if grau is None else round(grau, 1),
        area_bacia_km2=area_bacia_km2,
        volume_precipitado_m3=None if volume_precipitado is None else round(volume_precipitado),
        volume_escoado_m3=None if volume_escoado is None else round(volume_escoado),
        volume_infiltrado_m3=None if volume_infiltrado is None else round(volume_infiltrado),
        coeficiente_escoamento=coeficiente,
        precipitacao_total_mm=round(float(precipitacao_mm), 2),
        precipitacao_efetiva_mm=round(float(efetiva), 2),
        abstracao_inicial_mm=round(float(abstracao), 2),
        retencao_potencial_mm=round(float(retencao), 2),
        cn_base=round(float(cn_base), 1),
        cn_ajustado=round(float(cn_ajustado), 1),
        condicao_umidade=condicao,
        p72_mm=round(float(p72_mm), 2),
    )


# -----------------------------------------------------------------------------
# JANELA DE VULNERABILIDADE — o solo saturado demora a secar
# -----------------------------------------------------------------------------
# O ponto operacional mais importante do módulo, e o que justifica avisar ANTES
# da chuva cair.
#
# Uma vez saturado, o solo leva dias para voltar a absorver. Nesse intervalo,
# qualquer chuva nova escoa quase inteira — não há mais para onde a água ir.
# Foi exatamente o que ocorreu em Encantado em julho de 2026:
#
#     20/07   35,8 mm de chuva   saturação   2 %   cota    138 cm
#     21/07   69,6 mm de chuva   saturação 100 %   cota    771 cm
#     22/07   70,4 mm de chuva   saturação 100 %   cota  1.708 cm
#
# A chuva do dia 20 quase não moveu o rio: foi gasta encharcando o solo. Do dia
# 21 para o 22 choveu praticamente o mesmo (69,6 -> 70,4 mm), mas a cota MAIS
# QUE DOBROU, porque o solo já não absorvia nada.
#
# A diferença medida, para a mesma chuva:
#
#     20 mm  ->  escoa  0,0 mm (solo seco)  vs   2,7 mm (saturado)
#     40 mm  ->  escoa  0,0 mm (solo seco)  vs  14,2 mm (saturado)
#     60 mm  ->  escoa  1,1 mm (solo seco)  vs  29,3 mm (saturado)
#
# Por isso a saturação é sinal de alerta por si só, mesmo sem estar chovendo:
# ela diz que a bacia está armada.


def horas_ate_dessaturar(
    api_mm: float, meia_vida_dias: float = MEIA_VIDA_ENCHARCAMENTO_DIAS
) -> float | None:
    """Quanto tempo até o solo deixar de estar saturado, SE não chover mais.

    Invertendo o decaimento do índice de encharcamento:

        t = meia_vida · log2(API_atual / limiar_de_saturação)

    Devolve None quando o solo já não está saturado.
    """
    if api_mm is None or api_mm <= P72_AMC_UMIDA:
        return None
    return round(
        meia_vida_dias * 24.0 * math.log2(api_mm / P72_AMC_UMIDA), 1
    )


def simular_chuva(
    chuva_mm: float,
    api_atual_mm: float,
    cn_base: float = CN_PADRAO,
    area_bacia_km2: float | None = None,
) -> dict:
    """E se chovesse X mm agora, com o solo no estado em que está?

    Responde a pergunta que interessa antes do evento: a mesma chuva que hoje
    seria absorvida, amanhã — com o solo já encharcado — vira enxurrada. A
    comparação com o solo seco mostra o tamanho da diferença.
    """
    agora = calcular_chuva_efetiva(
        chuva_mm, p72_mm=api_atual_mm, cn_base=cn_base,
        api_mm=api_atual_mm, area_bacia_km2=area_bacia_km2,
    )
    seco = calcular_chuva_efetiva(
        chuva_mm, p72_mm=0.0, cn_base=cn_base,
        api_mm=0.0, area_bacia_km2=area_bacia_km2,
    )
    return {
        "chuva_simulada_mm": round(float(chuva_mm), 1),
        "escoaria_mm": agora.precipitacao_efetiva_mm,
        "escoaria_se_solo_seco_mm": seco.precipitacao_efetiva_mm,
        "volume_escoado_m3": agora.volume_escoado_m3,
        "coeficiente_escoamento": agora.coeficiente_escoamento,
        "vezes_mais_que_solo_seco": (
            round(agora.precipitacao_efetiva_mm / seco.precipitacao_efetiva_mm, 1)
            if seco.precipitacao_efetiva_mm > 0.05 else None
        ),
    }


def avaliar_vulnerabilidade(
    api_mm: float,
    cn_base: float = CN_PADRAO,
    area_bacia_km2: float | None = None,
    chuvas_teste: tuple[float, ...] = (10.0, 25.0, 50.0),
) -> dict:
    """Estado de armadilha da bacia: quão perigosa seria a PRÓXIMA chuva.

    Não depende de estar chovendo. É a leitura de que a bacia está armada — e
    de por quanto tempo ainda vai estar.
    """
    saturacao = float(np.clip(
        (api_mm - P72_AMC_SECA) / (P72_AMC_UMIDA - P72_AMC_SECA), 0.0, 1.0
    ))
    horas = horas_ate_dessaturar(api_mm)

    if saturacao >= 0.99:
        nivel, texto = "CRÍTICO", (
            "Solo saturado: chuva nova escoa quase inteira, sem absorção."
        )
    elif saturacao >= 0.6:
        nivel, texto = "ALTO", (
            "Solo próximo da saturação: pouca capacidade de absorção restante."
        )
    elif saturacao >= 0.25:
        nivel, texto = "MODERADO", "Solo parcialmente úmido."
    else:
        nivel, texto = "BAIXO", "Solo com boa capacidade de absorção."

    return {
        "encharcamento_mm": round(float(api_mm), 2),
        "saturacao_pct": round(saturacao * 100, 1),
        "nivel": nivel,
        "leitura": texto,
        "horas_ate_dessaturar": horas,
        "simulacoes": [
            simular_chuva(mm, api_mm, cn_base, area_bacia_km2)
            for mm in chuvas_teste
        ],
    }


def _serie_chuva_efetiva(
    acumulados: pd.DataFrame, cn_base: float, api: pd.Series | None = None
) -> pd.Series:
    """Chuva efetiva ao longo de toda a série, para virar preditor da regressão.

    Vetorizado: usa P24h como evento e P72h como saturação prévia.
    """
    p24 = acumulados["p24h"].to_numpy(dtype=float)
    p72 = acumulados["p72h"].to_numpy(dtype=float)

    cn_seco = 4.2 * cn_base / (10.0 - 0.058 * cn_base)
    cn_saturado = 23.0 * cn_base / (10.0 + 0.13 * cn_base)

    if api is not None:
        # Rampa contínua entre solo seco e encharcado, em vez do degrau de 72 h.
        grau = np.clip(
            (api.to_numpy(dtype=float) - P72_AMC_SECA)
            / (P72_AMC_UMIDA - P72_AMC_SECA),
            0.0, 1.0,
        )
        cn = cn_seco + grau * (cn_saturado - cn_seco)
    else:
        cn = np.where(
            p72 < P72_AMC_SECA, cn_seco,
            np.where(p72 > P72_AMC_UMIDA, cn_saturado, cn_base),
        )
    cn = np.clip(cn, 1.0, 100.0)

    retencao = 25400.0 / cn - 254.0
    excedente = p24 - 0.2 * retencao
    efetiva = np.where(excedente > 0, excedente ** 2 / (excedente + retencao), 0.0)
    return pd.Series(efetiva, index=acumulados.index, name="pe_mm")


# -----------------------------------------------------------------------------
# 5. REGRESSÃO CHUVA -> COTA FUTURA (numpy puro, sem sklearn)
# -----------------------------------------------------------------------------
def _minimos_quadrados_regularizados(
    X: np.ndarray, y: np.ndarray, lambda_ridge: float = LAMBDA_RIDGE
) -> np.ndarray:
    """Ridge fechada: beta = (XᵀX + λI)⁻¹ Xᵀy, com o intercepto fora da penalidade.

    Substitui `sklearn.linear_model.Ridge` — que não está instalado — sem perda
    prática nesta escala. `np.linalg.solve` com fallback para pseudo-inversa
    cobre o caso de matriz mal condicionada.
    """
    n_colunas = X.shape[1]
    penalidade = np.eye(n_colunas) * lambda_ridge
    penalidade[0, 0] = 0.0  # não penaliza o termo constante
    A = X.T @ X + penalidade
    b = X.T @ y
    try:
        return np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return np.linalg.pinv(A) @ b


# -----------------------------------------------------------------------------
# PROPAGAÇÃO DE ONDA DE CHEIA — montante -> jusante
# -----------------------------------------------------------------------------
# O ponto que faltava. Para estação de jusante, a chuva medida no próprio local
# quase não explica o nível: a água que passa ali caiu na cabeceira horas antes.
# Medido nos 30 dias do Taquari, prevendo Estrela:
#
#     chuva local de Estrela ......... r = 0,081   (inútil)
#     nível de Santa Tereza .......... r = 0,527
#     nível de Muçum ................. r = 0,602
#     nível de Encantado ............. r = 0,646   (8x melhor que a chuva local)
#
# E os tempos de viagem se somam, como onda de cheia deve fazer:
#
#     Santa Tereza -> Muçum 2,75 h -> Encantado 2,25 h -> Estrela 4,25 h
#              -> Bom Retiro do Sul 1,75 h -> Taquari 2,25 h   (soma 13,25 h)
#     Santa Tereza -> Taquari, medido direto ................. 14,25 h
#
# É isso que responde "quanto tempo a chuva que caiu lá em cima leva para
# chegar aqui embaixo" — a pergunta central do estudo.

# Só entram como preditor pares com correlação acima disto.
CORRELACAO_MINIMA_MONTANTE = 0.35

# Quantas estações de montante usar por estação (as de maior correlação).
MAXIMO_MONTANTE = 2

# Uma estação a jusante responde depois; lag zero significaria mesma seção.
LAG_MINIMO_HORAS = 0.25
LAG_MAXIMO_HORAS = 48.0


def _lag_entre_series(a: pd.Series, b: pd.Series,
                      maximo_horas: float = LAG_MAXIMO_HORAS) -> tuple[float, float]:
    """Defasagem que maximiza a correlação entre a SUBIDA de A e a de B.

    Correlaciona dH/dt e não o nível: o nível carrega o patamar do evento
    anterior e faria pares distantes parecerem correlacionados sem relação
    causal. A subida isola a passagem da onda.
    """
    idx = a.index.intersection(b.index)
    if len(idx) < 500:
        return 0.0, -1.0

    x = a.reindex(idx).interpolate(limit=8).diff().fillna(0.0).to_numpy()
    y = b.reindex(idx).interpolate(limit=8).diff().fillna(0.0).to_numpy()

    melhor_lag, melhor_r = 0.0, -1.0
    for passos in range(0, int(maximo_horas * PASSOS_POR_HORA) + 1):
        xa, ya = (x, y) if passos == 0 else (x[:-passos], y[passos:])
        if xa.std() == 0 or ya.std() == 0:
            continue
        r = float(np.corrcoef(xa, ya)[0, 1])
        if r > melhor_r:
            melhor_lag, melhor_r = passos / PASSOS_POR_HORA, r
    return melhor_lag, melhor_r


def descobrir_montante(
    estacao_id: str, db_path: str = CAMINHO_BANCO_PADRAO, dias: int = 30
) -> list[dict]:
    """Descobre, pelos dados, quais estações são de montante e o tempo de viagem.

    Não usa topologia de rede nem ordem de rio: varre as estações da MESMA
    bacia e mede qual delas antecipa a subida desta, e por quanto tempo. Sai
    ordenado por correlação.

    Devolve dicionários com `estacao_id`, `nome`, `lag_horas`, `correlacao` e
    a série já carregada em `_serie`, pronta para virar preditor.
    """
    cadastro = carregar_cadastro(estacao_id, db_path)
    bacia = cadastro.get("bacia")
    if not bacia:
        return []

    with _conectar(db_path) as con:
        vizinhas = con.execute(
            "SELECT id, nome FROM estacao WHERE bacia = ? AND id != ? "
            "AND fonte = 'SACE/SGB'",
            (bacia, estacao_id),
        ).fetchall()

    alvo = carregar_series_alinhadas(estacao_id, db_path, dias=dias)
    if alvo.empty or "cota_cm" not in alvo or alvo["cota_cm"].isna().all():
        return []

    achados: list[dict] = []
    for vid, vnome in vizinhas:
        serie = carregar_series_alinhadas(vid, db_path, dias=dias)
        if serie.empty or "cota_cm" not in serie or serie["cota_cm"].isna().all():
            continue

        lag, r = _lag_entre_series(serie["cota_cm"], alvo["cota_cm"])
        # lag > 0 e correlação alta = ela sobe ANTES desta: está a montante.
        if r >= CORRELACAO_MINIMA_MONTANTE and lag >= LAG_MINIMO_HORAS:
            achados.append({
                "estacao_id": vid,
                "nome": vnome,
                "lag_horas": round(lag, 2),
                "correlacao": round(r, 3),
                "_serie": serie["cota_cm"],
            })

    achados.sort(key=lambda d: -d["correlacao"])
    return achados[:MAXIMO_MONTANTE]


def _montar_preditores(
    df: pd.DataFrame,
    acumulados: pd.DataFrame,
    chuva_efetiva: pd.Series,
    montante: list[dict] | None = None,
    api: pd.Series | None = None,
) -> pd.DataFrame:
    """Matriz de preditores, alinhada no tempo.

    Colunas: cota atual, taxa de subida recente, acumulados de todas as janelas,
    chuva efetiva e — quando existirem — o NÍVEL DAS ESTAÇÕES DE MONTANTE,
    deslocado pelo tempo de viagem da onda.

    Por que montante entra: para estação de jusante, a chuva medida no próprio
    local quase não explica o nível. Medido nos 30 dias do Taquari, prevendo
    Estrela — chuva local r=0,08; nível de Encantado r=0,65, oito vezes melhor.
    A água que passa em Estrela caiu na Serra horas antes, não ali.

    Termo quadrático na chuva efetiva porque a relação volume -> cota é
    notoriamente não linear (a calha extravasa e a curva achata).
    """
    preditores = pd.DataFrame(index=df.index)
    preditores["cota_atual"] = df["cota_cm"]
    preditores["subida_3h"] = df["cota_cm"].diff(3 * PASSOS_POR_HORA)
    for horas in JANELAS_HORAS:
        preditores[f"p{horas}h"] = acumulados[f"p{horas}h"]
    preditores["pe_mm"] = chuva_efetiva
    preditores["pe_mm2"] = chuva_efetiva ** 2
    if api is not None:
        # Estado de encharcamento do solo. É o que faz a MESMA chuva gerar
        # cheia ou não: em Encantado, 68,8 mm com solo saturado deram 53 % de
        # escoamento; 1,4 mm com solo seco deram 0 %.
        preditores["encharcamento"] = api
        preditores["saturacao"] = np.clip(
            (api - P72_AMC_SECA) / (P72_AMC_UMIDA - P72_AMC_SECA), 0.0, 1.0
        )

    for i, mont in enumerate(montante or [], start=1):
        serie = mont.get("_serie")
        if serie is None or serie.empty:
            continue
        # Desloca pelo tempo de viagem: o que está subindo lá agora chega aqui
        # daqui a `lag_horas`, então é o valor de `lag_horas` ATRÁS que informa
        # o nível de agora.
        passos = max(1, int(round(mont["lag_horas"] * PASSOS_POR_HORA)))
        alinhada = serie.reindex(df.index).interpolate(limit=8)
        preditores[f"montante{i}_cota"] = alinhada.shift(passos)
        preditores[f"montante{i}_subida"] = (
            alinhada.diff(3 * PASSOS_POR_HORA).shift(passos)
        )

    return preditores


@dataclass
class ModeloHorizonte:
    """Modelo treinado para um horizonte de projeção específico."""
    beta: np.ndarray
    media: np.ndarray
    desvio: np.ndarray
    ganho_validacao: float       # ganho sobre persistência, FORA da amostra
    r2_validacao: float          # R² fora da amostra (referência secundária)
    r2_treino: float
    delta_minimo: float          # envelope físico observado no histórico
    delta_maximo: float
    n_amostras: int
    # Erro típico da previsão, em cm, medido FORA da amostra na validação
    # walk-forward. É o que dá largura ao envelope de incerteza: a projeção
    # central mais ou menos este valor cobre a faixa de erro observada.
    erro_cm: float = 0.0

    def prever_delta(self, x_atual: np.ndarray) -> float:
        """Variação de cota prevista, já limitada ao envelope histórico."""
        x_pad = np.concatenate([[1.0], (x_atual - self.media) / self.desvio])
        delta = float(x_pad @ self.beta)
        # Trava anti-extrapolação: o rio não pode variar mais do que já variou
        # em 30 dias neste mesmo horizonte. Sem isto o modelo linear dispara
        # (medido no retroteste: 7.781 cm projetados numa cheia que fez 1.708).
        return float(np.clip(delta, self.delta_minimo, self.delta_maximo))


def _treinar_horizonte(
    preditores: pd.DataFrame, alvo: pd.Series, passos_adiante: int
) -> ModeloHorizonte | None:
    """Treina a previsão de VARIAÇÃO de cota `passos_adiante` passos à frente.

    Duas decisões que mudam tudo em relação a uma regressão ingênua:

    1. O alvo é ΔH = H(t+k) − H(t), não H(t+k). Prever o nível absoluto parece
       ótimo (R² de treino ~0,94) porque o nível de agora já explica quase todo
       o nível daqui a pouco — o modelo aprende a copiar a entrada e o R² alto
       é ilusão. Prevendo a variação, o modelo é obrigado a extrair sinal da
       chuva, e o erro fica interpretável em centímetros.

    2. A validação é CRONOLÓGICA (treina nos primeiros 80 % do período, valida
       nos 20 % finais). Validação embaralhada vazaria futuro para o passado
       numa série temporal e devolveria um número bonito e mentiroso.
    """
    base = preditores.copy()
    base["_delta"] = alvo.shift(-passos_adiante) - alvo
    base = base.dropna()
    if len(base) < 200:
        return None

    y = base.pop("_delta").to_numpy(dtype=float)
    X = base.to_numpy(dtype=float)

    # --- Validação WALK-FORWARD (janela expansiva, 5 blocos contíguos).
    # Validar apenas nos 20 % finais deixaria a avaliação cair inteira na
    # vazante, onde a persistência é quase imbatível e qualquer modelo parece
    # ruim — a cheia, que é o que importa, nunca entraria na validação.
    # Aqui cada bloco vira validação uma vez, sempre treinando só com o
    # passado dele: sem vazamento de futuro e com o evento incluído.
    n_blocos = 5
    tamanho_bloco = len(X) // n_blocos
    erro_modelo_total = 0.0
    erro_persistencia_total = 0.0
    n_validacao = 0
    ss_res_total = 0.0
    ss_tot_total = 0.0

    for indice in range(1, n_blocos):
        fim_treino = tamanho_bloco * indice
        fim_val = fim_treino + tamanho_bloco if indice < n_blocos - 1 else len(X)
        if fim_treino < 100 or fim_val - fim_treino < 20:
            continue

        Xt, yt = X[:fim_treino], y[:fim_treino]
        Xv, yv = X[fim_treino:fim_val], y[fim_treino:fim_val]

        media_f = Xt.mean(axis=0)
        desvio_f = Xt.std(axis=0)
        desvio_f[desvio_f == 0] = 1.0
        montar = lambda b: np.column_stack(
            [np.ones(len(b)), (b - media_f) / desvio_f]
        )
        beta_f = _minimos_quadrados_regularizados(montar(Xt), yt)
        previsto_v = montar(Xv) @ beta_f

        erro_modelo_total += float(((yv - previsto_v) ** 2).sum())
        n_validacao += len(yv)
        erro_persistencia_total += float((yv ** 2).sum())   # persistência: ΔH = 0
        ss_res_total += float(((yv - previsto_v) ** 2).sum())
        ss_tot_total += float(((yv - yv.mean()) ** 2).sum())

    n_val = max(int(n_validacao), 1)
    erro_cm = float(np.sqrt(erro_modelo_total / n_val))

    ganho_walk = (
        1.0 - erro_modelo_total / erro_persistencia_total
        if erro_persistencia_total > 0 else 0.0
    )
    r2_walk = 1.0 - ss_res_total / ss_tot_total if ss_tot_total > 0 else 0.0

    # --- Modelo final: treinado em TODO o período disponível.
    media = X.mean(axis=0)
    desvio = X.std(axis=0)
    desvio[desvio == 0] = 1.0

    def padronizar(bloco: np.ndarray) -> np.ndarray:
        return np.column_stack([np.ones(len(bloco)), (bloco - media) / desvio])

    beta = _minimos_quadrados_regularizados(padronizar(X), y)
    X_treino, y_treino = X, y

    def r2(bloco: np.ndarray, observado: np.ndarray) -> float:
        previsto = padronizar(bloco) @ beta
        ss_res = float(((observado - previsto) ** 2).sum())
        ss_tot = float(((observado - observado.mean()) ** 2).sum())
        return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Envelope: percentis extremos da variação realmente observada.
    return ModeloHorizonte(
        beta=beta,
        media=media,
        desvio=desvio,
        ganho_validacao=ganho_walk,
        erro_cm=round(erro_cm, 1),
        r2_validacao=r2_walk,
        r2_treino=r2(X_treino, y_treino),
        delta_minimo=float(np.percentile(y, 0.5)),
        delta_maximo=float(np.percentile(y, 99.5)),
        n_amostras=len(base),
    )


# -----------------------------------------------------------------------------
# MODELO AGRUPADO — treinado com todas as estações ao mesmo tempo
# -----------------------------------------------------------------------------
# O salto de qualidade do módulo. Medido com validação "deixa uma estação de
# fora" (treina em 31 estações, prevê a 32ª, que o modelo nunca viu):
#
#     modelo por estação, 30 dias ......... ganho NEGATIVO (perde da persistência)
#     modelo agrupado, 107 mil amostras ... ganho +0,46
#
# A razão é simples: uma estação sozinha oferece ~2.900 instantes e
# essencialmente UM evento de cheia. Agrupando 32 estações, o mesmo evento é
# observado 32 vezes, em bacias de tamanhos e respostas diferentes — e aí há
# material para o ajuste generalizar em vez de decorar.
#
# SOBRE AS CONDICIONANTES DE SOLO, USO E GEOLOGIA
# ------------------------------------------------
# É neste modelo que elas PODEM pesar: por estação o CN é constante e uma
# coluna constante não informa nada numa regressão (variância zero, absorvida
# pelo intercepto). Agrupando, o CN varia entre estações e passa a poder
# explicar por que uma bacia responde diferente da outra.
#
# Medido, porém, o ganho delas é marginal:
#
#     só hidrologia ......................... 0,4601
#     + CN (solo + uso + litologia) ......... 0,4612
#     + densidade de drenagem ............... 0,4612
#     + densidade de lineamentos ............ 0,4615
#     + área da bacia ....................... 0,4615
#
# Total: +0,0014, ou 0,3%. Ficam no modelo porque não atrapalham e porque a
# tendência é ganharem peso conforme o arquivo mensal acumular mais eventos —
# mas registrar o tamanho real da contribuição evita atribuir a elas um mérito
# que hoje é do volume de dado.
#
# NORMALIZAÇÃO
# ------------
# Cada estação tem escala própria (Estrela opera na casa dos 2.000 cm, Linha
# Colombo dos 300 cm). Sem normalizar, o ajuste seria dominado pelos rios
# grandes. O alvo é a variação da cota RELATIVA à cota de inundação daquela
# estação, o que também torna o erro comparável entre bacias.

# Escala de densidade de drenagem do IBGE para número ordenável.
_DRENAGEM_ORDINAL = {
    "muito alta": 5, "alta": 4, "média": 3, "baixa": 2, "muito baixa": 1,
}

# Mínimo de amostras por estação para ela entrar no agrupamento.
MINIMO_AMOSTRAS_AGRUPADO = 100


@dataclass
class ModeloAgrupado:
    """Ajuste treinado sobre várias estações de uma vez."""
    beta: np.ndarray
    media: np.ndarray
    desvio: np.ndarray
    colunas: list[str]
    ganho_estacao_nova: float     # validação deixa-uma-estação-de-fora
    n_amostras: int
    n_estacoes: int
    horizonte_horas: float

    def prever_delta_relativo(self, x: np.ndarray) -> float:
        x_pad = np.concatenate([[1.0], (x - self.media) / self.desvio])
        return float(x_pad @ self.beta)


def _amostras_da_estacao(
    estacao: dict, db_path: str, passos_adiante: int, dias: int
) -> pd.DataFrame | None:
    """Monta as linhas de treino de UMA estação, já normalizadas."""
    inundacao = estacao.get("cota_inundacao_cm")
    if inundacao is None or pd.isna(inundacao) or float(inundacao) <= 0:
        return None  # sem cota oficial não há como normalizar

    serie = carregar_series_alinhadas(estacao["id"], db_path, dias=dias)
    if serie.empty or "cota_cm" not in serie:
        return None
    if serie["cota_cm"].isna().sum() > len(serie) * 0.5:
        return None

    acumulados = acumulados_moveis(serie["chuva_mm"])
    inundacao = float(inundacao)

    d = pd.DataFrame(index=serie.index)
    d["h_rel"] = serie["cota_cm"] / inundacao          # 1,0 = cota de inundação
    d["subida_rel"] = d["h_rel"].diff(3 * PASSOS_POR_HORA)
    for horas in JANELAS_HORAS:
        d[f"p{horas}h"] = acumulados[f"p{horas}h"]

    # --- Condicionantes da bacia (constantes aqui, variáveis entre estações)
    area_estacao = None
    try:
        import georisk_geo as gg
        cn = gg.cn_da_bacia(estacao.get("bacia"), db_path)
        carac = next(
            (c for c in gg.caracterizacoes(db_path)
             if gg._normalizar(c["nome"]) == gg._normalizar(estacao.get("bacia"))),
            None,
        )
        area_estacao = gg.area_da_estacao(estacao["id"], db_path)
    except Exception:
        cn, carac = None, None

    d["cn"] = cn if cn is not None else CN_PADRAO
    if carac:
        d["dens_drenagem"] = _DRENAGEM_ORDINAL.get(
            carac["geomorfologia"]["densidade_dominante"], 3
        )
        d["lineamentos"] = carac.get("densidade_lineamentos_km_km2", 0.0)
    else:
        d["dens_drenagem"], d["lineamentos"] = 3, 0.0

    # Área DRENADA PELA ESTAÇÃO, não pela bacia. Com a área da bacia esta
    # coluna era constante dentro de cada bacia — 26.315 km² tanto para Passo
    # Tainhas, que drena 1.120, quanto para Taquari, que drena 25.900 — e não
    # distinguia cabeceira de foz, que é justamente o que ela deveria trazer.
    # A ANA publica a área por estação; onde ela falta, cai na da bacia.
    if area_estacao:
        d["log_area"] = np.log10(max(area_estacao, 1.0))
    elif carac:
        d["log_area"] = np.log10(max(carac.get("area_km2", 1.0), 1.0))
    else:
        d["log_area"] = 3.0

    d["_alvo"] = d["h_rel"].shift(-passos_adiante) - d["h_rel"]
    d["_estacao"] = estacao["id"]
    d = d.dropna()
    return d if len(d) >= MINIMO_AMOSTRAS_AGRUPADO else None


_cache_agrupado: dict[float, "ModeloAgrupado | None"] = {}


def obter_modelo_agrupado(
    horizonte_horas: float = 6.0, db_path: str = CAMINHO_BANCO_PADRAO
) -> "ModeloAgrupado | None":
    """Modelo agrupado com cache — treinar varre a série de todas as estações."""
    chave = round(horizonte_horas, 2)
    if chave not in _cache_agrupado:
        try:
            _cache_agrupado[chave] = treinar_modelo_agrupado(horizonte_horas, db_path)
        except Exception:
            _cache_agrupado[chave] = None
    return _cache_agrupado[chave]


def treinar_modelo_agrupado(
    horizonte_horas: float = 6.0,
    db_path: str = CAMINHO_BANCO_PADRAO,
    dias: int = 30,
    progresso=None,
) -> ModeloAgrupado | None:
    """Treina um ajuste único sobre todas as estações com cota oficial.

    A validação é DEIXA-UMA-ESTAÇÃO-DE-FORA: treina em N-1 e mede na que ficou
    fora. É a pergunta certa — "este modelo serve para uma estação que ele nunca
    viu?" — e é bem mais dura que validar no tempo da mesma estação.
    """
    passos = max(1, int(round(horizonte_horas * PASSOS_POR_HORA)))

    with _conectar(db_path) as con:
        con.row_factory = sqlite3.Row
        estacoes = [
            dict(r) for r in con.execute(
                "SELECT id, nome, bacia, cota_inundacao_cm FROM estacao "
                "WHERE fonte = 'SACE/SGB' AND cota_inundacao_cm IS NOT NULL"
            )
        ]

    blocos = []
    for i, est in enumerate(estacoes):
        if progresso:
            try:
                progresso(i / max(len(estacoes), 1), f"Lendo {est['nome']}…")
            except Exception:
                pass
        bloco = _amostras_da_estacao(est, db_path, passos, dias)
        if bloco is not None:
            blocos.append(bloco)

    if len(blocos) < 5:
        return None

    dados = pd.concat(blocos)
    colunas = [c for c in dados.columns if not c.startswith("_")]

    X_todos = dados[colunas].to_numpy(dtype=float)
    y_todos = dados["_alvo"].to_numpy(dtype=float)

    def ajustar(X: np.ndarray, y: np.ndarray):
        media = X.mean(axis=0)
        desvio = X.std(axis=0)
        desvio[desvio == 0] = 1.0
        X_pad = np.column_stack([np.ones(len(X)), (X - media) / desvio])
        beta = _minimos_quadrados_regularizados(X_pad, y)
        return beta, media, desvio

    # --- Validação: cada estação, uma vez, fora do treino
    erro_modelo = erro_persistencia = 0.0
    for alvo_id in dados["_estacao"].unique():
        treino = dados[dados["_estacao"] != alvo_id]
        teste = dados[dados["_estacao"] == alvo_id]
        if len(teste) < MINIMO_AMOSTRAS_AGRUPADO:
            continue
        beta, media, desvio = ajustar(
            treino[colunas].to_numpy(float), treino["_alvo"].to_numpy()
        )
        Xv = teste[colunas].to_numpy(float)
        yv = teste["_alvo"].to_numpy()
        previsto = np.column_stack([np.ones(len(Xv)), (Xv - media) / desvio]) @ beta
        erro_modelo += float(((yv - previsto) ** 2).sum())
        erro_persistencia += float((yv ** 2).sum())

    ganho = (
        1.0 - erro_modelo / erro_persistencia if erro_persistencia > 0 else 0.0
    )

    beta, media, desvio = ajustar(X_todos, y_todos)
    return ModeloAgrupado(
        beta=beta, media=media, desvio=desvio, colunas=colunas,
        ganho_estacao_nova=round(ganho, 4),
        n_amostras=len(dados),
        n_estacoes=dados["_estacao"].nunique(),
        horizonte_horas=horizonte_horas,
    )


def projetar_com_agrupado(
    estacao_id: str,
    modelo: ModeloAgrupado,
    db_path: str = CAMINHO_BANCO_PADRAO,
    dias: int = 30,
) -> dict | None:
    """Aplica o modelo agrupado a uma estação, devolvendo a cota em cm."""
    cadastro = carregar_cadastro(estacao_id, db_path)
    passos = max(1, int(round(modelo.horizonte_horas * PASSOS_POR_HORA)))
    bloco = _amostras_da_estacao(cadastro, db_path, passos, dias)
    if bloco is None or bloco.empty:
        return None

    inundacao = float(cadastro["cota_inundacao_cm"])
    x = bloco[modelo.colunas].iloc[-1].to_numpy(dtype=float)
    delta_rel = modelo.prever_delta_relativo(x)

    cota_agora = float(bloco["h_rel"].iloc[-1]) * inundacao
    return {
        "cota_projetada_cm": round(max(0.0, cota_agora + delta_rel * inundacao), 1),
        "horas_a_frente": modelo.horizonte_horas,
        "ganho_estacao_nova": modelo.ganho_estacao_nova,
        "n_amostras_treino": modelo.n_amostras,
        "n_estacoes_treino": modelo.n_estacoes,
    }


# -----------------------------------------------------------------------------
# 6. GRÁFICO
# -----------------------------------------------------------------------------
def _saturacao_de(api: pd.Series) -> pd.Series:
    """Converte o índice de encharcamento em grau de saturação 0..1."""
    return ((api - P72_AMC_SECA) / (P72_AMC_UMIDA - P72_AMC_SECA)).clip(0.0, 1.0)


def grafico_hietograma_hidrograma(
    df: pd.DataFrame,
    cadastro: dict,
    projecao: pd.Series | None,
    tc_horas: float | None,
    dias_exibidos: int = 12,
) -> go.Figure:
    """Hietograma (barras, eixo invertido no topo) + hidrograma medido (linha)
    + hidrograma projetado (tracejado), deslocado no tempo pelo Tc.
    """
    # Fatiamento por índice em vez de DataFrame.last(), que saiu no pandas 3.0.
    if df.empty:
        recorte = df
    else:
        corte = df.index.max() - timedelta(days=dias_exibidos)
        recorte = df[df.index >= corte]

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    # --- Chuva: agregada por hora, senão viram 1.150 barrinhas ilegíveis.
    if not recorte.empty and recorte["chuva_mm"].sum() > 0:
        chuva_h = recorte["chuva_mm"].resample("h").sum()
        fig.add_trace(
            go.Bar(
                x=chuva_h.index, y=chuva_h.values, name="Chuva (mm/h)",
                marker_color="#4a6fa5", opacity=0.75,
                hovertemplate="%{x|%d/%m %H:%M}<br>%{y:.1f} mm<extra></extra>",
            ),
            secondary_y=True,
        )

    # --- Nível medido
    if "cota_cm" in recorte and recorte["cota_cm"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=recorte.index, y=recorte["cota_cm"], mode="lines",
                name="Nível medido (cm)", line=dict(color="#0066cc", width=3),
                hovertemplate="%{x|%d/%m %H:%M}<br>%{y:.0f} cm<extra></extra>",
            ),
            secondary_y=False,
        )

    # --- Nível projetado
    if projecao is not None and not projecao.empty:
        fig.add_trace(
            go.Scatter(
                x=projecao.index, y=projecao.values, mode="lines+markers",
                name=f"Nível projetado (Tc = {tc_horas:.1f} h)",
                line=dict(color="#d62728", width=3, dash="dash"),
                marker=dict(size=7, symbol="diamond"),
                hovertemplate="%{x|%d/%m %H:%M}<br>%{y:.0f} cm (projetado)<extra></extra>",
            ),
            secondary_y=False,
        )

    # --- Cotas oficiais do SACE
    for chave, cor, rotulo in (
        ("cota_atencao_cm", "#e6c200", "Atenção"),
        ("cota_alerta_cm", "#ff7f0e", "Alerta"),
        ("cota_inundacao_cm", "#d62728", "Inundação"),
    ):
        valor = cadastro.get(chave)
        if valor is not None and not pd.isna(valor):
            fig.add_hline(
                y=float(valor), line_color=cor, line_width=2, line_dash="dot",
                annotation_text=f"{rotulo}: {int(valor)} cm",
                annotation_position="top left", secondary_y=False,
            )

    nome = cadastro.get("nome", cadastro.get("id", "estação"))
    rio = cadastro.get("rio") or "rio não informado"
    subtitulo = (
        f"Tc = {tc_horas:.1f} h" if tc_horas is not None else "Tc indeterminado"
    )
    fig.update_layout(
        title=(
            f"<b>{nome} — Hietograma x Hidrograma</b><br>"
            f"<sup>{rio} · {subtitulo} · projeção deslocada no tempo pelo tempo "
            f"de resposta</sup>"
        ),
        height=520, hovermode="x unified", bargap=0.1,
        plot_bgcolor="#ffffff", paper_bgcolor="#ffffff",
        font=dict(color="#000000"),
        legend=dict(orientation="h", y=1.12, x=0.0),
        margin=dict(l=60, r=60, t=100, b=40),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#eaeaea", title_text="")
    fig.update_yaxes(
        title_text="<b>Nível (cm)</b>", secondary_y=False,
        showgrid=True, gridcolor="#eaeaea",
    )
    # Chuva invertida e ocupando a metade de cima: convenção de hietograma.
    if not recorte.empty and recorte["chuva_mm"].sum() > 0:
        pico = float(recorte["chuva_mm"].resample("h").sum().max())
        fig.update_yaxes(
            title_text="<b>Chuva (mm/h)</b>", secondary_y=True,
            range=[max(pico * 4, 1), 0], showgrid=False,
        )
    return fig


# -----------------------------------------------------------------------------
# 7. FUNÇÃO PRINCIPAL
# -----------------------------------------------------------------------------
def estimar_tempo_e_impacto_inundacao(
    estacao_id: str,
    db_path: str = CAMINHO_BANCO_PADRAO,
    cn_base: float | None = None,
    dias_historico: int = 30,
    ate_instante: str | pd.Timestamp | None = None,
) -> dict:
    """Estima Tc, projeta a cota futura e calcula o tempo até o extravasamento.

    Parâmetros
    ----------
    estacao_id : id da estação no banco (ex.: "SACE_taquari_2").
                 Use `estacoes_analisaveis()` para listar as elegíveis.
    db_path    : caminho do `georisk_rs.db`.
    cn_base    : Curve Number em AMC II. Se None (padrão), usa o CN REAL da
        bacia, derivado de pedologia e uso da terra do IBGE por
        `georisk_geo.cn_da_bacia()`. Só cai no valor genérico de 75 quando a
        bacia ainda não teve o CN calculado. Passe um número para forçar.
    dias_historico : janela de treino.
    ate_instante : corta a série neste instante e finge que "agora" é ele.
        Serve para RETROTESTE (rodar sobre uma cheia passada e comparar a
        projeção com o que de fato aconteceu) e para reproduzir um evento.
        None = usar o dado mais recente.

    Retorno (dict)
    --------------
    tempo_horas_ate_inundacao : horas até o pico projetado atingir a Cota de
        Inundação; None se não houver previsão de atingir (ou se faltar cota).
    cota_maxima_projetada_cm  : nível máximo estimado no horizonte.
    volume_efetivo_mm         : precipitação líquida (SCS-CN).
    grafico_hietograma_hidrograma : figura Plotly.
    ... além de tc_horas, correlação, projeção por horizonte, tempo até
    Atenção/Alerta, qualidade do ajuste, avisos e `confiavel`.

    IMPORTANTE: sempre cheque `confiavel` e `avisos` antes de usar os números.
    Quando a chuva local não comanda o rio (caso do baixo Uruguai), o módulo
    devolve `confiavel=False` — e aí a projeção não vale para decisão.
    """
    cadastro = carregar_cadastro(estacao_id, db_path)
    df = carregar_series_alinhadas(estacao_id, db_path, dias=dias_historico)

    # --- CN: real da bacia quando existir, senão o genérico documentado.
    cn_origem = "informado pelo usuário"
    if cn_base is None:
        cn_bacia = None
        try:
            import georisk_geo as gg
            cn_bacia = gg.cn_da_bacia(cadastro.get("bacia"), db_path)
        except Exception:
            cn_bacia = None
        if cn_bacia:
            cn_base, cn_origem = float(cn_bacia), "IBGE: pedologia + uso da terra da bacia"
        else:
            cn_base, cn_origem = CN_PADRAO, "genérico (bacia sem CN calculado)"


    if ate_instante is not None and not df.empty:
        corte = pd.Timestamp(ate_instante)
        df = df[df.index <= corte]
        if df.empty:
            raise ValueError(
                f"Não há dado desta estação até {corte}. "
                f"A série começa em {carregar_series_alinhadas(estacao_id, db_path).index.min()}."
            )

    resposta: dict = {
        "estacao_id": estacao_id,
        "nome": cadastro.get("nome"),
        "rio": cadastro.get("rio"),
        "bacia": cadastro.get("bacia"),
        "fonte": cadastro.get("fonte"),
        "cota_atual_cm": None,
        "medido_em": cadastro.get("medido_em"),
        "cota_atencao_cm": cadastro.get("cota_atencao_cm"),
        "cota_alerta_cm": cadastro.get("cota_alerta_cm"),
        "cota_inundacao_cm": cadastro.get("cota_inundacao_cm"),
        "tc_horas": None,
        "correlacao_chuva_nivel": None,
        "metodo_tc": None,
        "origem_chuva": None,
        "afericao_tc": None,
        "montante": [],
        "preditores_escolhidos": None,
        "preditores_n": None,
        "usou_montante": None,
        "projecao_agrupada": None,
        "origem_projecao": "estação",
        "precipitacao_acumulada_mm": {},
        "volume_efetivo_mm": None,
        "encharcamento_mm": None,
        "vulnerabilidade": None,
        "balanco_hidrico": None,
        "cn_base": None,
        "cn_origem": None,
        "cota_maxima_projetada_cm": None,
        "instante_pico_projetado": None,
        "pico_ja_ocorreu": None,
        "tendencia": None,
        "status_limiares": {},
        "projecao": [],
        "tempo_horas_ate_atencao": None,
        "tempo_horas_ate_alerta": None,
        "tempo_horas_ate_inundacao": None,
        "qualidade_ajuste_r2": None,
        "qualidade_ajuste_r2_treino": None,
        "ganho_sobre_persistencia": None,
        "horizonte_util_horas": None,
        "confiavel": False,
        "avisos": [],
        "grafico_hietograma_hidrograma": None,
    }

    if df.empty or df["cota_cm"].isna().all():
        resposta["avisos"].append(
            "Sem série de cota no banco para esta estação — não há o que projetar. "
            "Postos só de chuva não têm hidrograma."
        )
        resposta["grafico_hietograma_hidrograma"] = grafico_hietograma_hidrograma(
            df, cadastro, None, None
        )
        return resposta

    # --- Escolhe a chuva que de fato comanda esta estação
    chuva_usada, origem_chuva = escolher_chuva(df, cadastro.get("bacia"), db_path)
    df = df.assign(chuva_mm=chuva_usada.reindex(df.index).fillna(0.0))
    resposta["origem_chuva"] = origem_chuva

    # --- Acumulados e estado atual
    acumulados = acumulados_moveis(df["chuva_mm"])
    instante_atual = df.index[-1]
    resposta["cota_atual_cm"] = (
        None if pd.isna(df["cota_cm"].iloc[-1]) else round(float(df["cota_cm"].iloc[-1]), 1)
    )

    # --- IDADE DA ÂNCORA
    #
    # Toda a projeção parte do fim da série, não da leitura pontual do cadastro.
    # Quando a fonte para de publicar a série mas segue publicando a leitura, as
    # duas divergem e o painel mostrava projeção de dias atrás como se fosse de
    # agora — em agosto de 2026 o CSV de 15 min do SACE parou por quatro dias
    # enquanto a página do relatório continuava atualizando.
    #
    # Falha silenciosa é a pior espécie num sistema de decisão, então a idade da
    # âncora entra na resposta e derruba a confiabilidade quando passa do limite.
    resposta["ancora_em"] = str(instante_atual)
    idade_h = (pd.Timestamp.now() - instante_atual).total_seconds() / 3600.0
    resposta["idade_ancora_horas"] = round(idade_h, 1)
    resposta["ancora_defasada"] = bool(idade_h > IDADE_MAXIMA_ANCORA_HORAS)
    if resposta["ancora_defasada"]:
        resposta["avisos"].append(
            f"A série desta estação não é atualizada há {idade_h:.0f} h "
            f"(última em {instante_atual}). A projeção parte daí, não de agora — "
            f"é limitação da fonte, que parou de publicar a série de 15 min."
        )
    resposta["precipitacao_acumulada_mm"] = {
        f"{h}h": round(float(acumulados[f"p{h}h"].iloc[-1]), 2) for h in JANELAS_HORAS
    }

    # --- Encharcamento do solo, contínuo, a partir da série inteira de chuva
    api = indice_encharcamento(df["chuva_mm"])
    resposta["encharcamento_mm"] = round(float(api.iloc[-1]), 2)

    # --- Área da bacia, para converter lâmina (mm) em volume (m³)
    area_km2 = None
    try:
        import georisk_geo as gg
        carac = next(
            (c for c in gg.caracterizacoes(db_path)
             if gg._normalizar(c["nome"]) == gg._normalizar(cadastro.get("bacia"))),
            None,
        )
        if carac:
            area_km2 = carac.get("area_km2")
    except Exception:
        pass

    # --- Balanço volumétrico (SCS-CN com encharcamento contínuo e volume)
    balanco = calcular_chuva_efetiva(
        precipitacao_mm=float(acumulados["p24h"].iloc[-1]),
        p72_mm=float(acumulados["p72h"].iloc[-1]),
        cn_base=cn_base,
        api_mm=float(api.iloc[-1]),
        area_bacia_km2=area_km2,
    )
    resposta["vulnerabilidade"] = avaliar_vulnerabilidade(
        float(api.iloc[-1]), cn_base, area_km2
    )
    # O encharcamento NÃO é exibido em lugar nenhum — nem como aviso, nem como
    # marcação no gráfico. Ele age onde importa, silenciosamente:
    #
    #   - modula o CN de forma contínua (`_ajustar_cn_por_umidade`), o que muda
    #     retenção, abstração inicial e portanto a chuva efetiva;
    #   - entra como preditor candidato, e a seleção por validação decide se
    #     fica;
    #   - define o volume escoado do balanço.
    #
    # Ou seja: as estimativas já saem considerando o solo. O resultado do
    # cálculo continua disponível em `resposta["vulnerabilidade"]` para quem
    # quiser consultar por código, mas nada disso vai para a tela.

    resposta["cn_base"] = round(float(cn_base), 1)
    resposta["cn_origem"] = cn_origem
    resposta["volume_efetivo_mm"] = balanco.precipitacao_efetiva_mm
    resposta["balanco_hidrico"] = balanco.__dict__.copy()

    # --- Tempo de resposta
    tempo = estimar_tempo_resposta(df)
    resposta["tc_horas"] = tempo.tc_horas
    resposta["correlacao_chuva_nivel"] = round(tempo.correlacao, 3)
    resposta["metodo_tc"] = tempo.metodo
    resposta["avisos"].extend(tempo.avisos)

    # --- Aferição independente do Tc pela densidade de drenagem da bacia.
    # É CONFERÊNCIA, não correção: o Tc continua saindo da correlação cruzada.
    # Divergência aqui costuma ser informativa, não erro — estação de jusante
    # soma o tempo de propagação no canal, que a relação de drenagem não cobre.
    try:
        import georisk_geo as gg
        resposta["afericao_tc"] = gg.conferir_tc(
            cadastro.get("bacia"), tempo.tc_horas, db_path
        )
    except Exception:
        resposta["afericao_tc"] = None

    if tempo.tc_horas is None or tempo.tc_horas <= 0:
        resposta["grafico_hietograma_hidrograma"] = grafico_hietograma_hidrograma(
            df, cadastro, None, tempo.tc_horas
        )
        return resposta

    # --- Regressão por horizonte
    chuva_efetiva = _serie_chuva_efetiva(acumulados, cn_base, api)

    # Estações de montante: é o preditor que faz a projeção funcionar em
    # jusante, onde a chuva local não explica nada.
    try:
        montante = descobrir_montante(estacao_id, db_path, dias=dias_historico)
    except Exception:
        montante = []
    resposta["montante"] = [
        {k: v for k, v in m.items() if not k.startswith("_")} for m in montante
    ]

    preditores_completos = _montar_preditores(
        df, acumulados, chuva_efetiva, montante, api
    )

    # --- SELEÇÃO DO CONJUNTO DE PREDITORES POR VALIDAÇÃO
    #
    # Mais condicionantes NÃO significa mais confiável — é o contrário quando
    # os dados são poucos. Medido neste próprio projeto, prevendo Encantado a
    # 6 h com validação walk-forward:
    #
    #     2 preditores (cota + subida) ......... ganho -2,317
    #     7 preditores (+ chuva, 5 janelas) .... ganho -0,122   <- melhor
    #     9 preditores (+ chuva efetiva/CN) .... ganho -0,525
    #    13 preditores (+ montante) ............ ganho -4,299   <- 35x pior
    #
    # Com 30 dias e essencialmente um evento de cheia, cada variável a mais
    # compra ajuste ao passado e paga com generalização. Por isso o conjunto
    # não é fixado no chute: testamos os candidatos e ficamos com o que se sai
    # melhor FORA da amostra, nesta estação e neste momento.
    #
    # É também o que permite a propagação de montante entrar onde ela ajuda
    # (jusante, durante evento) sem estragar onde ela atrapalha.
    colunas_base = ["cota_atual", "subida_3h"]
    colunas_chuva = [f"p{h}h" for h in JANELAS_HORAS]
    colunas_cn = ["pe_mm", "pe_mm2"]
    colunas_solo = [c for c in ("encharcamento", "saturacao")
                    if c in preditores_completos.columns]
    colunas_montante = [c for c in preditores_completos.columns if c.startswith("montante")]

    candidatos: dict[str, list[str]] = {
        "cota + chuva": colunas_base + colunas_chuva,
        "cota + chuva + CN": colunas_base + colunas_chuva + colunas_cn,
        "cota + chuva + CN + encharcamento":
            colunas_base + colunas_chuva + colunas_cn + colunas_solo,
        "somente cota": colunas_base,
    }
    if colunas_montante:
        candidatos["cota + chuva + CN + montante"] = (
            colunas_base + colunas_chuva + colunas_cn + colunas_montante
        )
        candidatos["cota + montante"] = colunas_base + colunas_montante

    passos_teste = max(
        1, int(round(tempo.tc_horas * 0.5 * PASSOS_POR_HORA))
    )
    melhor_nome, melhor_ganho, melhor_cols = None, -np.inf, None
    for nome_conj, cols in candidatos.items():
        cols = [c for c in cols if c in preditores_completos.columns]
        if not cols:
            continue
        modelo_teste = _treinar_horizonte(
            preditores_completos[cols], df["cota_cm"], passos_teste
        )
        if modelo_teste is None:
            continue
        if modelo_teste.ganho_validacao > melhor_ganho:
            melhor_nome, melhor_ganho = nome_conj, modelo_teste.ganho_validacao
            melhor_cols = cols

    if melhor_cols is None:
        melhor_cols = [c for c in candidatos["cota + chuva"]
                       if c in preditores_completos.columns]
        melhor_nome = "cota + chuva (padrão)"

    resposta["preditores_escolhidos"] = melhor_nome
    resposta["preditores_n"] = len(melhor_cols)
    resposta["usou_montante"] = any(c.startswith("montante") for c in melhor_cols)

    preditores = preditores_completos[melhor_cols]
    x_atual = preditores.iloc[-1]

    if x_atual.isna().any():
        resposta["avisos"].append(
            "Preditores do instante atual incompletos (falha recente de sensor); "
            "a projeção foi omitida."
        )
        resposta["grafico_hietograma_hidrograma"] = grafico_hietograma_hidrograma(
            df, cadastro, None, tempo.tc_horas
        )
        return resposta

    x_atual_np = x_atual.to_numpy(dtype=float)
    cota_agora = float(df["cota_cm"].iloc[-1])
    # Teto físico: nem a maior cheia dos 30 dias mais 20 % de folga. Trava o
    # que o clip por horizonte deixar passar.
    teto = float(df["cota_cm"].max()) * 1.2

    instantes, valores, r2_val, r2_tre, ganhos = [], [], [], [], []
    ganho_por_horizonte: list[float] = []
    erros: list[float] = []

    for fracao in FRACOES_HORIZONTE:
        horas = tempo.tc_horas * fracao
        passos = max(1, int(round(horas * PASSOS_POR_HORA)))
        modelo = _treinar_horizonte(preditores, df["cota_cm"], passos)
        if modelo is None:
            continue
        # Projeta a VARIAÇÃO e soma à cota de agora: a projeção fica ancorada
        # no nível medido, em vez de flutuar livre.
        projetado = cota_agora + modelo.prever_delta(x_atual_np)
        instantes.append(instante_atual + timedelta(hours=horas))
        valores.append(float(np.clip(projetado, 0.0, teto)))
        erros.append(modelo.erro_cm)
        r2_val.append(modelo.r2_validacao)
        r2_tre.append(modelo.r2_treino)
        ganhos.append(modelo.ganho_validacao)
        ganho_por_horizonte.append(round(modelo.ganho_validacao, 3))

    if not instantes:
        resposta["avisos"].append(
            "Histórico insuficiente para treinar a regressão em qualquer horizonte."
        )
        resposta["grafico_hietograma_hidrograma"] = grafico_hietograma_hidrograma(
            df, cadastro, None, tempo.tc_horas
        )
        return resposta

    projecao = pd.Series(valores, index=pd.DatetimeIndex(instantes), name="cota_projetada")
    # O R² que reportamos e usamos para decidir confiabilidade e o de VALIDAÇÃO
    # (fora da amostra). O de treino vai junto só para você ver a diferença.
    resposta["qualidade_ajuste_r2"] = round(float(np.mean(r2_val)), 3)
    resposta["qualidade_ajuste_r2_treino"] = round(float(np.mean(r2_tre)), 3)
    resposta["ganho_sobre_persistencia"] = round(float(np.mean(ganhos)), 3)
    # `ganho` por horizonte: acima de 0 o modelo supera a persistência NAQUELE
    # alcance. Serve para você saber até onde a projeção tem valor — na prática
    # os horizontes curtos sustentam bem melhor que os longos.
    resposta["projecao"] = [
        {
            "instante": instante.strftime("%Y-%m-%d %H:%M:%S"),
            "horas_a_frente": round((instante - instante_atual).total_seconds() / 3600, 2),
            "cota_projetada_cm": round(valor, 1),
            # ENVELOPE DE INCERTEZA: a projeção central mais ou menos o erro
            # típico medido fora da amostra. Não é intervalo de confiança
            # formal — é a faixa de erro que o modelo de fato cometeu na
            # validação walk-forward, que é o que se pode afirmar com os dados.
            "cota_minima_cm": round(max(0.0, valor - (erros[i] if i < len(erros) else 0)), 1),
            "cota_maxima_cm": round(valor + (erros[i] if i < len(erros) else 0), 1),
            "erro_tipico_cm": round(erros[i], 1) if i < len(erros) else None,
            "ganho_sobre_persistencia": (
                ganho_por_horizonte[i] if i < len(ganho_por_horizonte) else None
            ),
        }
        for i, (instante, valor) in enumerate(projecao.items())
    ]

    # --- Tendência: o rio está subindo ou vazando?
    cota_agora = float(df["cota_cm"].iloc[-1])
    # Usa o horizonte MAIS CURTO: o de longo prazo é o mais incerto e inverter
    # o sinal da tendência por causa dele daria leitura errada da situação.
    variacao = float(projecao.iloc[0]) - cota_agora
    if variacao > 5:
        resposta["tendencia"] = "subindo"
    elif variacao < -5:
        resposta["tendencia"] = "recessão (vazante)"
    else:
        resposta["tendencia"] = "estável"

    # O pico relevante inclui o nível de agora: se o rio já está vazando, o
    # máximo do evento ficou para trás e projetar um "pico futuro" menor que o
    # atual seria enganoso.
    pico = max(float(projecao.max()), cota_agora)
    resposta["cota_maxima_projetada_cm"] = round(pico, 1)
    resposta["pico_ja_ocorreu"] = bool(float(projecao.max()) <= cota_agora)
    resposta["instante_pico_projetado"] = (
        instante_atual.strftime("%Y-%m-%d %H:%M:%S")
        if resposta["pico_ja_ocorreu"]
        else projecao.idxmax().strftime("%Y-%m-%d %H:%M:%S")
    )

    # --- Tempo até CRUZAR cada cota oficial
    # Só faz sentido falar em "tempo até atingir" para limiar ainda NÃO
    # atingido. Se o rio já está acima, o tempo é zero e o que importa é a
    # informação de que a cota está ultrapassada agora.
    resposta["status_limiares"] = {}
    for chave, saida, rotulo in (
        ("cota_atencao_cm", "tempo_horas_ate_atencao", "atenção"),
        ("cota_alerta_cm", "tempo_horas_ate_alerta", "alerta"),
        ("cota_inundacao_cm", "tempo_horas_ate_inundacao", "inundação"),
    ):
        limiar = cadastro.get(chave)
        if limiar is None or pd.isna(limiar):
            resposta["status_limiares"][rotulo] = "sem cota oficial publicada"
            continue

        limiar = float(limiar)
        if cota_agora >= limiar:
            resposta[saida] = 0.0
            resposta["status_limiares"][rotulo] = "JÁ ULTRAPASSADA"
            continue

        cruzamentos = projecao[projecao >= limiar]
        if cruzamentos.empty:
            resposta["status_limiares"][rotulo] = "não atingida no horizonte projetado"
            continue

        primeiro = cruzamentos.index[0]
        resposta[saida] = round((primeiro - instante_atual).total_seconds() / 3600, 2)
        resposta["status_limiares"][rotulo] = (
            f"projetada para daqui a {resposta[saida]:.1f} h"
        )

    if cadastro.get("cota_inundacao_cm") is None or pd.isna(cadastro.get("cota_inundacao_cm")):
        resposta["avisos"].append(
            "Estação sem cota de inundação oficial publicada — não dá para calcular "
            "tempo até extravasar. Só o SACE publica esse limiar."
        )

    # HORIZONTE ÚTIL: até onde a projeção realmente vale.
    # Medido no retroteste da cheia de 22/07 em Estrela: ganho +0,46 em 4 h,
    # +0,28 em 8,5 h, e negativo de 12,75 h em diante. Ou seja, a projeção tem
    # valor operacional na primeira metade do Tc e vira ruído depois. Reprovar
    # o modelo inteiro por causa da cauda descartaria a parte que funciona;
    # informar o alcance é mais útil e mais honesto.
    # SEQUÊNCIA a partir do horizonte mais curto, não o maior positivo isolado.
    #
    # Antes eu pegava max() dos horizontes com ganho positivo. Isso dava
    # resultado fisicamente impossível: em Estrela, ganho -11,0 em 4 h, -5,2 em
    # 8,6 h, -1,4 em 12,9 h e então +0,30 em 21,6 h — e o campo anunciava
    # "útil até 21,6 h". Se o modelo erra feio no curto prazo, aquele positivo
    # lá na ponta é ruído, não habilidade: previsão não melhora com o alcance.
    #
    # Agora o horizonte útil é o fim da sequência ININTERRUPTA de ganhos
    # positivos começando pelo mais curto. Primeiro negativo, para.
    uteis: list[float] = []
    for ponto in resposta["projecao"]:
        if (ponto.get("ganho_sobre_persistencia") or -1) > 0:
            uteis.append(ponto["horas_a_frente"])
        else:
            break
    resposta["horizonte_util_horas"] = max(uteis) if uteis else None

    resposta["confiavel"] = bool(tempo.confiavel and uteis)

    # --- MODELO AGRUPADO como alternativa quando o por-estação não serve.
    #
    # Com 30 dias, uma estação sozinha oferece essencialmente um evento de
    # cheia e o ajuste perde para a persistência. Agrupando 44 estações, o
    # mesmo evento é observado dezenas de vezes em bacias diferentes, e a
    # validação deixa-uma-estação-de-fora dá ganho +0,47 numa estação que o
    # modelo nunca viu. Então: se o local não bate a persistência, oferecemos
    # o agrupado, que bate.
    if not resposta["confiavel"]:
        # O modelo agrupado ASSUME a projeção, não fica de nota de rodapé.
        #
        # Antes o painel dizia "sem confiabilidade estatística" mesmo quando o
        # agrupado tinha ganho +0,52 prevendo estação que nunca viu — e ainda
        # exibia a projeção ruim da estação sozinha. Era pessimismo indevido:
        # há um modelo validado disponível, e é ele que deve responder.
        #
        # A validação do agrupado é deixa-uma-estação-de-fora: treina em N-1
        # estações e mede na que sobrou. Para uma estação cuja chuva local não
        # explica o nível (r=0,08 em Passo Carreiro), isso é exatamente o caso
        # de uso — o agrupado aprende o comportamento de bacias parecidas.
        horizontes = [
            max(1.0, (tempo.tc_horas or 6.0) * f) for f in (0.25, 0.5, 1.0)
        ]
        pontos_agrupado = []
        ganho_agrupado = None

        for horas in horizontes:
            modelo_ag = obter_modelo_agrupado(horizonte_horas=horas, db_path=db_path)
            if modelo_ag is None or modelo_ag.ganho_estacao_nova <= 0:
                continue
            previsto = projetar_com_agrupado(estacao_id, modelo_ag, db_path)
            if not previsto:
                continue
            ganho_agrupado = modelo_ag.ganho_estacao_nova
            pontos_agrupado.append({
                "instante": (
                    instante_atual + timedelta(hours=horas)
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "horas_a_frente": round(horas, 2),
                "cota_projetada_cm": previsto["cota_projetada_cm"],
                "cota_minima_cm": previsto["cota_projetada_cm"],
                "cota_maxima_cm": previsto["cota_projetada_cm"],
                "erro_tipico_cm": None,
                "ganho_sobre_persistencia": round(modelo_ag.ganho_estacao_nova, 3),
            })

        if pontos_agrupado:
            resposta["projecao"] = pontos_agrupado
            resposta["origem_projecao"] = "modelo agrupado"
            resposta["ganho_sobre_persistencia"] = round(ganho_agrupado, 3)
            resposta["horizonte_util_horas"] = max(
                p["horas_a_frente"] for p in pontos_agrupado
            )
            resposta["confiavel"] = True

            projecao = pd.Series(
                [p["cota_projetada_cm"] for p in pontos_agrupado],
                index=pd.DatetimeIndex([p["instante"] for p in pontos_agrupado]),
            )
            pico_ag = max(float(projecao.max()), cota_agora)
            resposta["cota_maxima_projetada_cm"] = round(pico_ag, 1)
            resposta["pico_ja_ocorreu"] = bool(float(projecao.max()) <= cota_agora)

            variacao_ag = float(projecao.iloc[0]) - cota_agora
            resposta["tendencia"] = (
                "subindo" if variacao_ag > 5
                else "recessão (vazante)" if variacao_ag < -5 else "estável"
            )

            # Refaz o tempo até cada cota com a projeção que vale agora.
            resposta["status_limiares"] = {}
            for chave, saida, rotulo in (
                ("cota_atencao_cm", "tempo_horas_ate_atencao", "atenção"),
                ("cota_alerta_cm", "tempo_horas_ate_alerta", "alerta"),
                ("cota_inundacao_cm", "tempo_horas_ate_inundacao", "inundação"),
            ):
                limiar = cadastro.get(chave)
                resposta[saida] = None
                if limiar is None or pd.isna(limiar):
                    resposta["status_limiares"][rotulo] = "sem cota oficial publicada"
                    continue
                limiar = float(limiar)
                if cota_agora >= limiar:
                    resposta[saida] = 0.0
                    resposta["status_limiares"][rotulo] = "JÁ ULTRAPASSADA"
                    continue
                cruza = projecao[projecao >= limiar]
                if cruza.empty:
                    resposta["status_limiares"][rotulo] = (
                        "não atingida no horizonte projetado"
                    )
                else:
                    horas_ate = (
                        cruza.index[0] - instante_atual
                    ).total_seconds() / 3600
                    resposta[saida] = round(horas_ate, 2)
                    resposta["status_limiares"][rotulo] = (
                        f"projetada para daqui a {horas_ate:.1f} h"
                    )

            resposta["avisos"].append(
                f"A chuva medida nesta estação não explica o nível dela "
                f"(r={tempo.correlacao:.2f}), então a projeção vem do MODELO "
                f"AGRUPADO: treinado com {modelo_ag.n_amostras:,} amostras de "
                f"{modelo_ag.n_estacoes} estações, com ganho "
                f"{ganho_agrupado:+.2f} sobre a persistência prevendo estação "
                f"que nunca viu."
            )

    # Âncora velha derruba a confiabilidade, venha a projeção de onde vier.
    # Fica por último de propósito: o modelo agrupado marca `confiavel = True`
    # quando assume, e sem esta linha uma série parada há dias voltaria a ser
    # apresentada como projeção boa.
    if resposta.get("ancora_defasada"):
        resposta["confiavel"] = False

    resposta["grafico_hietograma_hidrograma"] = grafico_hietograma_hidrograma(
        df, cadastro, projecao, tempo.tc_horas
    )
    return resposta


# -----------------------------------------------------------------------------
# CACHE DE PROJEÇÕES — para o mapa mostrar "vai subir", não "está alto"
# -----------------------------------------------------------------------------
# Projetar uma estação custa ~1,2 s. As 59 levam 1,2 min — inviável a cada
# rerun do painel, e é por isso que o mapa nascia colorido pela leitura atual.
# A saída é calcular fora da interação e guardar.

# Frações da amplitude recente que separam as classes de subida. São
# PARÂMETRO, escolhidos para dar uma escala legível, não limiar medido.
FAIXAS_SUBIDA = ((0.50, "forte"), (0.25, "moderada"), (0.10, "leve"))


def _classificar_subida(
    cota_atual: float | None,
    pico_projetado: float | None,
    serie_cota: pd.Series | None,
    cotas_oficiais: dict | None = None,
) -> tuple[str, str]:
    """Classe de risco da PROJEÇÃO, em duas camadas.

    **Oficial primeiro.** Se a estação tem cota publicada pelo SACE, a classe é
    a maior cota que o pico projetado alcança. É a mesma hierarquia das manchas:
    onde existe limiar oficial, ele manda.

    **Amplitude recente como régua de reserva.** Só 4 das 59 estações
    analisáveis têm cota oficial, e apenas 5 têm histórico longo — não há
    percentil confiável nem limiar para as outras 54. Sobra a única régua que
    todas possuem: o quanto o próprio rio variou na janela de 30 dias. A subida
    projetada é expressa como fração de `p95 − p05` dessa janela.

    Isso responde "vai subir muito para o que este rio costuma fazer?", que não
    é a mesma pergunta que "vai extravasar" — e a legenda do painel diz isso com
    todas as letras. Onde a janela não contém evento, a amplitude é pequena e a
    classe exagera; é limitação declarada, não estimativa disfarçada.
    """
    if cota_atual is None or pico_projetado is None:
        return "indefinida", "sem projeção"

    for campo, classe, rotulo in (
        ("cota_inundacao_cm", "inundacao", "pico atinge a cota de inundação"),
        ("cota_alerta_cm", "alerta", "pico atinge a cota de alerta"),
        ("cota_atencao_cm", "atencao", "pico atinge a cota de atenção"),
    ):
        limiar = (cotas_oficiais or {}).get(campo)
        if limiar is not None and not pd.isna(limiar) and pico_projetado >= limiar:
            return classe, rotulo

    if cotas_oficiais and any(
        v is not None and not pd.isna(v) for v in cotas_oficiais.values()
    ):
        return "abaixo", "pico abaixo de todas as cotas oficiais"

    if serie_cota is None or len(serie_cota.dropna()) < 50:
        return "indefinida", "série curta demais para uma régua"

    limpa = serie_cota.dropna()
    amplitude = float(limpa.quantile(0.95) - limpa.quantile(0.05))
    if amplitude <= 0:
        return "indefinida", "rio sem variação na janela"

    fracao = (pico_projetado - cota_atual) / amplitude
    for corte, nome in FAIXAS_SUBIDA:
        if fracao >= corte:
            return f"subida_{nome}", (
                f"subida {nome} — {fracao * 100:.0f} % da amplitude de 30 dias"
            )
    return "estavel", f"variação de {fracao * 100:.0f} % da amplitude de 30 dias"


COLUNAS_PROJECAO = (
    "id_estacao", "nome", "municipio", "bacia", "calculado_em",
    "chuva_24h_mm", "chuva_72h_mm", "volume_escoado_m3", "cn_base",
    "cota_atual_cm", "pico_projetado_cm", "variacao_cm",
    "tc_horas", "instante_pico", "horizonte_util_horas",
    "confiavel", "origem_projecao", "tendencia",
    "classe", "motivo", "horas_ate_inundacao",
)


def criar_schema_projecao(db_path: str = CAMINHO_BANCO_PADRAO) -> None:
    """Cria a tabela do cache, recriando-a se o conjunto de colunas mudou.

    É cache puro — reconstruído em 1,2 min por `atualizar_projecoes()` — então
    descartar na mudança de esquema é mais honesto que migrar: evita que uma
    coluna nova fique nula em metade das linhas e o painel mostre buraco sem
    explicar por quê.
    """
    with _conectar(db_path) as con:
        existentes = [
            c[1] for c in con.execute("PRAGMA table_info(projecao_cache)").fetchall()
        ]
        if existentes and set(existentes) != set(COLUNAS_PROJECAO):
            con.execute("DROP TABLE projecao_cache")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS projecao_cache (
                id_estacao   TEXT PRIMARY KEY,
                nome         TEXT,
                municipio    TEXT,
                bacia        TEXT,
                calculado_em TEXT,
                chuva_24h_mm          REAL,
                chuva_72h_mm          REAL,
                volume_escoado_m3     REAL,
                cn_base               REAL,
                cota_atual_cm         REAL,
                pico_projetado_cm     REAL,
                variacao_cm           REAL,
                tc_horas              REAL,
                instante_pico         TEXT,
                horizonte_util_horas  REAL,
                confiavel             INTEGER,
                origem_projecao       TEXT,
                tendencia             TEXT,
                classe                TEXT,
                motivo                TEXT,
                horas_ate_inundacao   REAL
            )
            """
        )


def atualizar_projecoes(
    db_path: str = CAMINHO_BANCO_PADRAO,
    cn_base: float = CN_PADRAO,
    verboso: bool = False,
) -> pd.DataFrame:
    """Projeta todas as estações analisáveis e grava o resultado.

    Roda fora da interação — pela CLI (`--projetar`) ou pelo coletor. O painel
    só lê. Estação que falha é registrada com classe `indefinida` em vez de
    derrubar a rodada inteira: uma fonte com CSV corrompido não pode apagar as
    outras 58 do mapa.
    """
    criar_schema_projecao(db_path)
    elegiveis = estacoes_analisaveis(db_path)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    linhas = []

    for n, r in enumerate(elegiveis.itertuples(), start=1):
        registro = {
            "id_estacao": r.id, "nome": r.nome, "municipio": r.municipio,
            "bacia": r.bacia,
            "calculado_em": agora,
            "chuva_24h_mm": None, "chuva_72h_mm": None,
            "volume_escoado_m3": None, "cn_base": None,
            "cota_atual_cm": None, "pico_projetado_cm": None, "variacao_cm": None,
            "tc_horas": None, "instante_pico": None,
            "horizonte_util_horas": None, "confiavel": 0,
            "origem_projecao": None, "tendencia": None,
            "classe": "indefinida", "motivo": "não calculada",
            "horas_ate_inundacao": None,
        }
        try:
            res = estimar_tempo_e_impacto_inundacao(r.id, db_path=db_path, cn_base=cn_base)
            serie = carregar_series_alinhadas(r.id, db_path=db_path)
            classe, motivo = _classificar_subida(
                res.get("cota_atual_cm"), res.get("cota_maxima_projetada_cm"),
                serie["cota_cm"] if serie is not None and not serie.empty else None,
                {
                    "cota_atencao_cm": r.cota_atencao_cm,
                    "cota_alerta_cm": r.cota_alerta_cm,
                    "cota_inundacao_cm": r.cota_inundacao_cm,
                },
            )
            atual, pico = res.get("cota_atual_cm"), res.get("cota_maxima_projetada_cm")
            acum = res.get("precipitacao_acumulada_mm") or {}
            bal = res.get("balanco_hidrico") or {}
            registro.update({
                "chuva_24h_mm": acum.get("24h"),
                "chuva_72h_mm": acum.get("72h"),
                "volume_escoado_m3": bal.get("volume_escoado_m3"),
                "cn_base": res.get("cn_base"),
                "cota_atual_cm": atual,
                "pico_projetado_cm": pico,
                "variacao_cm": None if atual is None or pico is None else pico - atual,
                "tc_horas": res.get("tc_horas"),
                "instante_pico": res.get("instante_pico_projetado"),
                "horizonte_util_horas": res.get("horizonte_util_horas"),
                "confiavel": int(bool(res.get("confiavel"))),
                "origem_projecao": res.get("origem_projecao"),
                "tendencia": res.get("tendencia"),
                "classe": classe, "motivo": motivo,
                "horas_ate_inundacao": res.get("tempo_horas_ate_inundacao"),
            })
        except Exception as erro:
            registro["motivo"] = f"falhou: {type(erro).__name__}"

        linhas.append(registro)
        if verboso:
            print(f"  [{n:>3}/{len(elegiveis)}] {r.nome[:26]:28s} "
                  f"{registro['classe']:16s} {registro['motivo']}")

    tabela = pd.DataFrame(linhas)
    with _conectar(db_path) as con:
        con.executemany(
            "INSERT OR REPLACE INTO projecao_cache ("
            + ",".join(COLUNAS_PROJECAO) + ") VALUES ("
            + ",".join(f":{c}" for c in COLUNAS_PROJECAO) + ")",
            linhas,
        )
    return tabela


def carregar_projecoes(db_path: str = CAMINHO_BANCO_PADRAO) -> pd.DataFrame:
    """Cache de projeções. Vazio (sem erro) quando ainda não foi calculado."""
    criar_schema_projecao(db_path)
    with _conectar(db_path) as con:
        return pd.read_sql_query("SELECT * FROM projecao_cache", con)


# -----------------------------------------------------------------------------
# EXECUÇÃO DIRETA — diagnóstico rápido
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Análise hidrológica GeoRisk-RS.")
    ap.add_argument("estacao", nargs="?", help="id da estação (ex.: SACE_taquari_2)")
    ap.add_argument("--listar", action="store_true", help="listar estações analisáveis")
    ap.add_argument("--projetar", action="store_true",
                    help="projetar todas as estações e gravar o cache do mapa")
    ap.add_argument("--exportar", metavar="CSV",
                    help="com --projetar, grava também o resultado neste CSV")
    ap.add_argument("--banco", default=CAMINHO_BANCO_PADRAO)
    ap.add_argument("--cn", type=float, default=CN_PADRAO)
    args = ap.parse_args()

    if args.projetar:
        print("Projetando todas as estações analisáveis (~1,2 s cada)…")
        tabela = atualizar_projecoes(args.banco, cn_base=args.cn, verboso=True)
        contagem = tabela["classe"].value_counts()
        print("\nResumo:")
        for classe, n in contagem.items():
            print(f"  {n:>3}  {classe}")
        if args.exportar:
            Path(args.exportar).parent.mkdir(parents=True, exist_ok=True)
            tabela.to_csv(args.exportar, index=False, encoding="utf-8")
            print(f"\nCSV gravado em {args.exportar}")
        raise SystemExit(0)

    if args.listar or not args.estacao:
        tabela = estacoes_analisaveis(args.banco)
        colunas = ["id", "nome", "rio", "chuva_mm_periodo", "cota_inundacao_cm"]
        print(tabela[colunas].to_string(index=False))
        raise SystemExit(0)

    resultado = estimar_tempo_e_impacto_inundacao(
        args.estacao, db_path=args.banco, cn_base=args.cn
    )
    figura = resultado.pop("grafico_hietograma_hidrograma")
    print(json.dumps(resultado, indent=2, ensure_ascii=False, default=str))
    if figura is not None:
        destino = f"hidrograma_{args.estacao}.html"
        figura.write_html(destino)
        print(f"\nGráfico salvo em: {destino}")
