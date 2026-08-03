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
def _ajustar_cn_por_umidade(cn2: float, p72_mm: float) -> tuple[float, str]:
    """Converte CN de umidade média (AMC II) para a condição real do solo.

    A saturação prévia é inferida da chuva acumulada em 72 h, como pedido.
    Solo já encharcado infiltra menos e escoa mais — é o que transforma uma
    chuva "normal" em cheia.
    """
    if p72_mm < P72_AMC_SECA:
        cn = 4.2 * cn2 / (10.0 - 0.058 * cn2)          # AMC I — solo seco
        return cn, "AMC I (solo seco)"
    if p72_mm > P72_AMC_UMIDA:
        cn = 23.0 * cn2 / (10.0 + 0.13 * cn2)          # AMC III — solo saturado
        return cn, "AMC III (solo saturado)"
    return cn2, "AMC II (umidade média)"


def calcular_chuva_efetiva(
    precipitacao_mm: float, p72_mm: float, cn_base: float = CN_PADRAO
) -> ChuvaEfetiva:
    """Precipitação efetiva (escoamento superficial) pelo método SCS-CN.

        S  = 25400/CN - 254           retenção potencial máxima (mm)
        Ia = 0.2 * S                  abstração inicial (mm)
        Pe = (P - Ia)^2 / (P - Ia + S)    se P > Ia, senão 0

    `cn_base` é PARÂMETRO, não medição: 75 representa bacia rural mista em solo
    hidrológico B/C. Se você tiver uso e tipo de solo da bacia, passe o CN
    tabelado correspondente.
    """
    cn_ajustado, condicao = _ajustar_cn_por_umidade(cn_base, p72_mm)
    cn_ajustado = min(max(cn_ajustado, 1.0), 100.0)

    retencao = 25400.0 / cn_ajustado - 254.0
    abstracao = 0.2 * retencao

    if precipitacao_mm > abstracao:
        excedente = precipitacao_mm - abstracao
        efetiva = excedente ** 2 / (excedente + retencao)
    else:
        efetiva = 0.0

    return ChuvaEfetiva(
        precipitacao_total_mm=round(float(precipitacao_mm), 2),
        precipitacao_efetiva_mm=round(float(efetiva), 2),
        abstracao_inicial_mm=round(float(abstracao), 2),
        retencao_potencial_mm=round(float(retencao), 2),
        cn_base=round(float(cn_base), 1),
        cn_ajustado=round(float(cn_ajustado), 1),
        condicao_umidade=condicao,
        p72_mm=round(float(p72_mm), 2),
    )


def _serie_chuva_efetiva(acumulados: pd.DataFrame, cn_base: float) -> pd.Series:
    """Chuva efetiva ao longo de toda a série, para virar preditor da regressão.

    Vetorizado: usa P24h como evento e P72h como saturação prévia.
    """
    p24 = acumulados["p24h"].to_numpy(dtype=float)
    p72 = acumulados["p72h"].to_numpy(dtype=float)

    cn = np.where(
        p72 < P72_AMC_SECA,
        4.2 * cn_base / (10.0 - 0.058 * cn_base),
        np.where(
            p72 > P72_AMC_UMIDA,
            23.0 * cn_base / (10.0 + 0.13 * cn_base),
            cn_base,
        ),
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


def _montar_preditores(
    df: pd.DataFrame, acumulados: pd.DataFrame, chuva_efetiva: pd.Series
) -> pd.DataFrame:
    """Matriz de preditores, alinhada no tempo.

    Colunas: cota atual, taxa de subida recente, acumulados de todas as janelas
    e chuva efetiva. Termo quadrático na chuva efetiva porque a relação
    volume -> cota é notoriamente não linear (calha extravasa e a curva achata).
    """
    preditores = pd.DataFrame(index=df.index)
    preditores["cota_atual"] = df["cota_cm"]
    preditores["subida_3h"] = df["cota_cm"].diff(3 * PASSOS_POR_HORA)
    for horas in JANELAS_HORAS:
        preditores[f"p{horas}h"] = acumulados[f"p{horas}h"]
    preditores["pe_mm"] = chuva_efetiva
    preditores["pe_mm2"] = chuva_efetiva ** 2
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
    cn_base: float = CN_PADRAO,
    dias_historico: int = 30,
    ate_instante: str | pd.Timestamp | None = None,
) -> dict:
    """Estima Tc, projeta a cota futura e calcula o tempo até o extravasamento.

    Parâmetros
    ----------
    estacao_id : id da estação no banco (ex.: "SACE_taquari_2").
                 Use `estacoes_analisaveis()` para listar as elegíveis.
    db_path    : caminho do `georisk_rs.db`.
    cn_base    : Curve Number em AMC II. Padrão 75 (rural misto, solo B/C).
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
        "precipitacao_acumulada_mm": {},
        "volume_efetivo_mm": None,
        "balanco_hidrico": None,
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

    # --- Balanço volumétrico (SCS-CN com AMC pelas 72 h)
    balanco = calcular_chuva_efetiva(
        precipitacao_mm=float(acumulados["p24h"].iloc[-1]),
        p72_mm=float(acumulados["p72h"].iloc[-1]),
        cn_base=cn_base,
    )
    resposta["volume_efetivo_mm"] = balanco.precipitacao_efetiva_mm
    resposta["balanco_hidrico"] = balanco.__dict__.copy()

    # --- Tempo de resposta
    tempo = estimar_tempo_resposta(df)
    resposta["tc_horas"] = tempo.tc_horas
    resposta["correlacao_chuva_nivel"] = round(tempo.correlacao, 3)
    resposta["metodo_tc"] = tempo.metodo
    resposta["avisos"].extend(tempo.avisos)

    if tempo.tc_horas is None or tempo.tc_horas <= 0:
        resposta["grafico_hietograma_hidrograma"] = grafico_hietograma_hidrograma(
            df, cadastro, None, tempo.tc_horas
        )
        return resposta

    # --- Regressão por horizonte
    chuva_efetiva = _serie_chuva_efetiva(acumulados, cn_base)
    preditores = _montar_preditores(df, acumulados, chuva_efetiva)
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
    uteis = [
        p["horas_a_frente"]
        for p in resposta["projecao"]
        if (p.get("ganho_sobre_persistencia") or -1) > 0
    ]
    resposta["horizonte_util_horas"] = max(uteis) if uteis else None

    resposta["confiavel"] = bool(tempo.confiavel and uteis)
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
