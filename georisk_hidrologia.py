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
from dataclasses import dataclass, field
from datetime import timedelta
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
def _conectar(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"Banco não encontrado em {db_path}. Rode a coleta primeiro: "
            "`python georisk_dados.py --exportar`."
        )
    return sqlite3.connect(db_path, timeout=30)


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
    """
    with _conectar(db_path) as con:
        df = pd.read_sql_query(
            """
            SELECT e.id, e.nome, e.rio, e.bacia, e.fonte,
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
            f"Correlação fraca (r={melhor_r:.2f} < {CORRELACAO_MINIMA:.2f}): o nível "
            "desta estação não é comandado pela chuva medida nela mesma — típico "
            "de rio grande, cujo nível vem de chuva a centenas de km a montante. "
            "A projeção NÃO deve ser usada para decisão."
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
        erro_persistencia_total += float((yv ** 2).sum())   # persistência: ΔH = 0
        ss_res_total += float(((yv - previsto_v) ** 2).sum())
        ss_tot_total += float(((yv - yv.mean()) ** 2).sum())

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
    try:
        import georisk_geo as gg
        cn = gg.cn_da_bacia(estacao.get("bacia"), db_path)
        carac = next(
            (c for c in gg.caracterizacoes(db_path)
             if gg._normalizar(c["nome"]) == gg._normalizar(estacao.get("bacia"))),
            None,
        )
    except Exception:
        cn, carac = None, None

    d["cn"] = cn if cn is not None else CN_PADRAO
    if carac:
        d["dens_drenagem"] = _DRENAGEM_ORDINAL.get(
            carac["geomorfologia"]["densidade_dominante"], 3
        )
        d["lineamentos"] = carac.get("densidade_lineamentos_km_km2", 0.0)
        d["log_area"] = np.log10(max(carac.get("area_km2", 1.0), 1.0))
    else:
        d["dens_drenagem"], d["lineamentos"], d["log_area"] = 3, 0.0, 3.0

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
        "afericao_tc": None,
        "montante": [],
        "preditores_escolhidos": None,
        "preditores_n": None,
        "usou_montante": None,
        "projecao_agrupada": None,
        "precipitacao_acumulada_mm": {},
        "volume_efetivo_mm": None,
        "encharcamento_mm": None,
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

    # --- Acumulados e estado atual
    acumulados = acumulados_moveis(df["chuva_mm"])
    instante_atual = df.index[-1]
    resposta["cota_atual_cm"] = (
        None if pd.isna(df["cota_cm"].iloc[-1]) else round(float(df["cota_cm"].iloc[-1]), 1)
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
        agrupado = obter_modelo_agrupado(
            horizonte_horas=max(1.0, (tempo.tc_horas or 6.0) * 0.5), db_path=db_path
        )
        if agrupado is not None and agrupado.ganho_estacao_nova > 0:
            alternativa = projetar_com_agrupado(estacao_id, agrupado, db_path)
            if alternativa:
                resposta["projecao_agrupada"] = alternativa
                resposta["avisos"].append(
                    f"O ajuste desta estação sozinha não supera a persistência, mas o "
                    f"MODELO AGRUPADO supera: treinado com "
                    f"{agrupado.n_amostras:,} amostras de {agrupado.n_estacoes} estações, "
                    f"tem ganho {agrupado.ganho_estacao_nova:+.2f} prevendo estação que "
                    f"nunca viu. Projeção dele: "
                    f"{alternativa['cota_projetada_cm']:.0f} cm em "
                    f"+{alternativa['horas_a_frente']:.0f} h."
                )
    if uteis:
        resposta["avisos"].append(
            f"Projeção com ganho real sobre a persistência até {max(uteis):.1f} h à "
            f"frente. Além disso o modelo perde para simplesmente supor que o nível "
            f"fica como está — não use os horizontes mais longos para decisão."
        )
    if not uteis and tempo.confiavel:
        resposta["avisos"].append(
            f"O modelo não superou a persistência fora da amostra "
            f"(ganho={resposta['ganho_sobre_persistencia']}). Com 30 dias e poucos "
            "eventos de cheia, prever \"o nível fica como está\" acerta mais. Trate a "
            "projeção como ordem de grandeza, não como valor."
        )

    resposta["grafico_hietograma_hidrograma"] = grafico_hietograma_hidrograma(
        df, cadastro, projecao, tempo.tc_horas
    )
    return resposta


# -----------------------------------------------------------------------------
# EXECUÇÃO DIRETA — diagnóstico rápido
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Análise hidrológica GeoRisk-RS.")
    ap.add_argument("estacao", nargs="?", help="id da estação (ex.: SACE_taquari_2)")
    ap.add_argument("--listar", action="store_true", help="listar estações analisáveis")
    ap.add_argument("--banco", default=CAMINHO_BANCO_PADRAO)
    ap.add_argument("--cn", type=float, default=CN_PADRAO)
    args = ap.parse_args()

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
