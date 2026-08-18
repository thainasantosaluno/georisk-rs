"""
GeoRisk-RS — Sala de Situação SACE / SGB
=========================================
Mesma estrutura visual de antes (indicadores no topo, aba de estações com
filtro por bacia e tabelas laterais, aba de manchas em satélite), mas o
`CATALOGO_SACE` fixo foi REMOVIDO: não há mais nível, chuva nem status
inventado no código.

Tudo vem do banco `georisk_rs.db`, alimentado por `georisk_dados.py` a partir
das fontes oficiais publicadas em HTML/CSV/XML:
  - SACE / SGB-CPRM  -> nível, cotas oficiais, situação e série de 15 min;
  - ANA / Telemetria -> estações automáticas do RS (nível, vazão, chuva).

Este painel compartilha o mesmo banco e o mesmo coletor do `main.py`. Atualize
por qualquer um dos dois — o outro enxerga na hora.

Rodar:  streamlit run dashboard.py
"""

from __future__ import annotations

import warnings
from datetime import timedelta

import folium
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from streamlit_folium import st_folium

import georisk_dados as gd
import georisk_hidrologia as gh
import georisk_geo as gg
import georisk_mapa as gm

warnings.filterwarnings("ignore")

# -----------------------------------------------------------------------------
# 1. CONFIGURAÇÃO PÁGINA
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="GeoRisk-RS — Sala de Situação SACE / SGB",
    page_icon="🌊",
    layout="wide",
)

st.title("🌊 GeoRisk-RS — Sala de Situação e Monitoramento de Cheias")
st.caption(
    "Monitoramento fluviométrico e pluviométrico com dado real do SACE/SGB-CPRM "
    "e da telemetria da ANA — nada de valor sintético."
)

MUNICIPIOS_RS_COORDS = {
    "Porto Alegre": [-30.0346, -51.2177],
    "Lajeado": [-29.4667, -51.9667],
    "Muçum": [-29.1667, -51.8667],
    "Encantado": [-29.2333, -51.8667],
    "Santa Tereza": [-29.1667, -51.7333],
    "São Leopoldo": [-29.7547, -51.1472],
    "Campo Bom": [-29.6833, -51.0500],
    "Taquara": [-29.6500, -50.7833],
    "Montenegro": [-29.6833, -51.4667],
    "São Sebastião do Caí": [-29.5833, -51.3833],
    "Eldorado do Sul": [-30.0847, -51.3325],
    "Rio Pardo": [-29.9833, -52.3833],
    "Cachoeira do Sul": [-30.0333, -52.9000],
    "Caxias do Sul": [-29.1642, -51.1308],
    "Santa Maria": [-29.7236, -53.7169],
    "Uruguaiana": [-29.7547, -57.0883],
    "Estrela": [-29.5000, -51.9667],
}


# -----------------------------------------------------------------------------
# COMPORTAMENTO DA ATUALIZAÇÃO — fixo, sem controle na tela
# -----------------------------------------------------------------------------
# Estes dois eram caixas de seleção na barra lateral. Viraram constantes porque
# a escolha errada tinha consequência silenciosa e ninguém percebia:
#
#   - deixar a ANA de fora produzia o descompasso que o aviso de defasagem
#     denuncia: SACE de hoje e 500 estações de anteontem no mesmo mapa, com
#     aparência de dado atual;
#   - deixar a atualização desligada fazia o painel mostrar dado velho sem
#     nenhum sinal disso.
#
# 15 min é o piso útil: o SACE republica nesse ritmo, então buscar mais rápido
# não traz nada novo e só bate no servidor deles à toa.
#
# Para mudar, edite aqui — é decisão de projeto, não de uso no dia a dia.
INTERVALO_ATUALIZACAO_MIN = 15
INCLUIR_ANA = True


# -----------------------------------------------------------------------------
# 2. CARREGAMENTO DOS DADOS REAIS (substitui o antigo CATALOGO_SACE)
# -----------------------------------------------------------------------------
@st.cache_data(ttl=30)
def carregar_dados_integrados() -> pd.DataFrame:
    """Lê do banco. Quem popula o banco é `georisk_dados.sincronizar()`."""
    return gd.carregar_estacoes()


@st.cache_data(ttl=120)
def carregar_serie(id_estacao: str, grandeza: str) -> pd.DataFrame:
    return gd.carregar_serie(id_estacao, grandeza)


def atualizar(incluir_ana: bool) -> None:
    barra = st.progress(0.0, text="Iniciando coleta…")
    try:
        gd.sincronizar(
            incluir_ana=incluir_ana,
            progresso=lambda f, m: barra.progress(min(max(f, 0.0), 1.0), text=m),
        )
    except Exception as exc:
        barra.empty()
        st.error(f"Falha na coleta: {type(exc).__name__}: {exc}")
        return
    barra.empty()
    st.cache_data.clear()
    st.rerun()


with st.sidebar:
    st.header("🔄 Dados")
    ultima = gd.ultima_coleta()
    idade = gd.idade_dados()
    if ultima:
        st.caption(
            f"Última coleta: **{ultima['terminada_em']}**"
            + (f" ({int(idade.total_seconds() // 60)} min atrás)" if idade else "")
            + f"\n\n{ultima['fontes']} — {ultima['estacoes']} estações"
        )
    else:
        st.warning("Banco vazio. Rode uma coleta.")

    st.caption(
        f"🔄 Atualização **automática** a cada **{INTERVALO_ATUALIZACAO_MIN} min**, "
        "com SACE + telemetria da ANA."
    )


    st.divider()
    st.caption(
        "Cotas de atenção/alerta/inundação são as **oficiais do SACE**, lidas "
        "junto com o nível. Estação sem cota publicada aparece cinza — nunca "
        "arbitramos um limiar."
    )


@st.fragment(run_every="60s")
def vigia() -> None:
    """Acorda a cada minuto e coleta quando o dado passa do intervalo."""
    atual = gd.idade_dados()
    if atual is None or atual > timedelta(minutes=INTERVALO_ATUALIZACAO_MIN):
        with st.spinner("Atualização automática…"):
            gd.sincronizar(incluir_ana=INCLUIR_ANA)
        st.cache_data.clear()
        st.rerun(scope="app")


df_estacoes = carregar_dados_integrados()

if df_estacoes.empty:
    st.warning(
        "Nenhuma estação no banco ainda. Clique em **Atualizar agora** na barra "
        "lateral para buscar os dados reais das fontes oficiais."
    )
    st.stop()


def texto(valor, sufixo: str = "", casas: int = 0) -> str:
    if valor is None or pd.isna(valor):
        return "Sem dado"
    return f"{float(valor):.{casas}f}{sufixo}"


def ou(valor, padrao: str = "—") -> str:
    """Texto do banco com fallback. Campo vazio chega do SQLite como NaN, que
    e 'verdadeiro' em Python — por isso `valor or padrao` nao serve aqui."""
    if valor is None or pd.isna(valor) or str(valor).strip() == "":
        return padrao
    return str(valor)


# -----------------------------------------------------------------------------
# 3. PAINEL SUPERIOR DE INDICADORES
# -----------------------------------------------------------------------------


gm.avisar_defasagem(df_estacoes)

c_m1, c_m2, c_m3, c_m4, c_m5 = st.columns(5)
c_m1.metric("Estações Monitoradas", len(df_estacoes))
c_m2.metric("Inundação 🔴", int((df_estacoes["cor"] == "red").sum()))
c_m3.metric("Alerta 🟧", int((df_estacoes["cor"] == "orange").sum()))
c_m4.metric("Atenção 🟨", int((df_estacoes["cor"] == "gold").sum()))
c_m5.metric("Maior Chuva 24h", texto(df_estacoes["chuva_24h"].max(), " mm", 1))

st.divider()


# -----------------------------------------------------------------------------
# 4. MORFOLOGIA DAS MANCHAS (esquemático)
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# 5. GRÁFICO ESTILO SACE — agora com a SÉRIE REAL
# -----------------------------------------------------------------------------
def gerar_grafico_estilo_sace(row: pd.Series) -> go.Figure | None:
    serie_cota = carregar_serie(row["id"], "cota")
    serie_chuva = carregar_serie(row["id"], "chuva")
    if serie_cota.empty and serie_chuva.empty:
        return None

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    if not serie_cota.empty:
        fig.add_trace(
            go.Scatter(
                x=serie_cota["datahora"], y=serie_cota["valor"],
                mode="lines", name="Cota (cm)",
                line=dict(color="#0066cc", width=3),
            ),
            secondary_y=False,
        )

    if not serie_chuva.empty:
        chuva_h = (
            serie_chuva.set_index("datahora")["valor"].resample("h").sum().reset_index()
        )
        fig.add_trace(
            go.Bar(
                x=chuva_h["datahora"], y=chuva_h["valor"],
                name="Chuva (mm/h)", marker_color="#666666", opacity=0.7,
            ),
            secondary_y=True,
        )

    # Só desenha o limiar que a fonte realmente publicou.
    for chave, cor, rotulo in [
        ("cota_atencao_cm", "#e6c200", "Cota de Atenção"),
        ("cota_alerta_cm", "#ff7f0e", "Cota de Alerta"),
        ("cota_inundacao_cm", "#d62728", "Cota de Inundação"),
    ]:
        valor = row.get(chave)
        if pd.notna(valor):
            fig.add_hline(
                y=float(valor), line_color=cor, line_width=2.5, line_dash="dash",
                annotation_text=f"{rotulo}: {int(valor)} cm",
                annotation_position="top right", secondary_y=False,
            )

    fig.update_layout(
        title=(
            f"<b>{row['nome']} — Cota (cm) e Chuva (mm)</b><br>"
            f"<sup>Medição da fonte: {ou(row.get('medido_em'), 'sem registro')} — "
            f"Status: {row.get('situacao')} — Fonte: {row['fonte']}</sup>"
        ),
        height=470, hovermode="x unified", bargap=0.05,
        legend=dict(orientation="h", y=1.14, x=0.0, font=dict(color="#000000", size=12)),
        plot_bgcolor="#ffffff", paper_bgcolor="#ffffff",
        font=dict(color="#000000", family="Arial"),
        margin=dict(l=40, r=40, t=90, b=40),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e0e0e0", tickfont=dict(color="#000000"))
    fig.update_yaxes(
        title_text="<b>Cota (cm)</b>", tickfont=dict(color="#000000"),
        secondary_y=False, showgrid=True, gridcolor="#e0e0e0",
    )
    fig.update_yaxes(
        title_text="<b>Chuva (mm/h)</b>", tickfont=dict(color="#000000"),
        secondary_y=True, showgrid=False,
    )
    return fig


@st.dialog("📋 Boletim SACE / Defesa Civil — dado real", width="large")
def exibir_boletim_modal(row: pd.Series) -> None:
    st.markdown(f"### Estação: {row['nome']}")
    st.markdown(
        f"**Bacia:** `{ou(row.get('bacia'))}` | "
        f"**Rio:** `{ou(row.get('rio'), 'Não informado')}` | "
        f"**Fonte:** `{row['fonte']}` | "
        f"**Código:** `{ou(row.get('codigo'))}`"
    )

    # Quando o valor atual falta mas a série tem histórico, mostra a última
    # leitura conhecida e a idade dela. "Sem dado" ao lado de um gráfico cheio
    # de pontos confundia: parecia ausência de medição, quando na verdade é
    # estação que parou de transmitir.
    nivel_txt, nota_nivel = gm.texto_com_idade(
        row.get("nivel_cm"), row["id"], "cota", " cm")
    chuva_txt, nota_chuva = gm.texto_com_idade(
        row.get("chuva_24h"), row["id"], "chuva", " mm", 1)

    # A vazão medida só existe onde a ANA publica: zero das 59 estações do
    # SACE têm o campo. Onde falta, a curva-chave da PRÓPRIA estação — ajustada
    # sobre milhares de pares medidos de cota x vazão — preenche, e o rótulo
    # diz que é estimativa, com o erro típico junto.
    vazao_medida = row.get("vazao_m3s")
    estimada = None
    if (vazao_medida is None or pd.isna(vazao_medida)) and row.get("codigo"):
        try:
            estimada = gd.vazao_estimada(row["codigo"], row.get("nivel_cm"))
        except Exception:
            estimada = None

    k1, k2, k3 = st.columns(3)
    k1.metric("Nível", nivel_txt, help=nota_nivel)
    k2.metric("Chuva 24h", chuva_txt, help=nota_chuva)
    if estimada:
        # O rótulo "estimada" fica; a margem sai da tela. Mostrar "±18 %" ao
        # lado do número poluía a leitura de quem só precisa da ordem de
        # grandeza — o dado continua auditável no `help` e no `curva_chave`.
        k3.metric(
            "Vazão estimada", f"{estimada['vazao_m3s']:,.0f} m³/s".replace(",", "."),
            help="Não medida aqui. Estimada pela curva-chave da própria "
                 "estação, ajustada sobre pares medidos de cota × vazão.",
        )
    else:
        k3.metric("Vazão", texto(vazao_medida, " m³/s", 1))

    nota = nota_nivel or nota_chuva
    if nota:
        st.warning(f"⚠️ Estação sem transmissão: {nota}.", icon="⚠️")

    # pd.notna: vindo do banco, campo vazio chega como NaN (que é "verdadeiro").
    if pd.notna(row.get("observacao")):
        st.warning(f"Valor descartado por implausibilidade física: {row['observacao']}")

    figura = gerar_grafico_estilo_sace(row)
    if figura is None:
        st.info("Ainda sem série histórica no banco para esta estação.")
    else:
        st.plotly_chart(figura, width='stretch')

    serie_cota = carregar_serie(row["id"], "cota")
    serie_chuva = carregar_serie(row["id"], "chuva")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button(
            "📥 Dados Cota (CSV)",
            data=(
                serie_cota.to_csv(index=False).encode("utf-8")
                if not serie_cota.empty else b"sem dado\n"
            ),
            file_name=f"cota_{row['id']}.csv", mime="text/csv",
            width='stretch', disabled=serie_cota.empty,
        )
    with c2:
        st.download_button(
            "📥 Dados Chuva (CSV)",
            data=(
                serie_chuva.to_csv(index=False).encode("utf-8")
                if not serie_chuva.empty else b"sem dado\n"
            ),
            file_name=f"chuva_{row['id']}.csv", mime="text/csv",
            width='stretch', disabled=serie_chuva.empty,
        )
    with c3:
        if estimada:
            # A validação (r², erro mediano, faixa de validade) continua toda no
            # cálculo: `ajustar_curva_chave` reprova curva ruim e
            # `vazao_estimada` recusa extrapolação. O que sai daqui é só a
            # exibição desses números — a decisão de mostrar ou não o valor
            # segue sendo tomada por eles.
            st.info(
                f"Vazão **estimada**: {estimada['vazao_m3s']:,.0f} m³/s"
                .replace(",", ".")
                + "\n\nCurva-chave da própria estação, ajustada sobre pares "
                  "medidos de cota × vazão."
            )
        else:
            st.info(f"Vazão medida: **{texto(row.get('vazao_m3s'), ' m³/s', 1)}**")


# -----------------------------------------------------------------------------
# 6. ABAS

# -----------------------------------------------------------------------------
# MANCHAS OFICIAIS — georisk_geo (SGB/IPH-UFRGS + Defesa Civil RS)
# -----------------------------------------------------------------------------











# -----------------------------------------------------------------------------
tab_mapa, tab_manchas, tab_hidro = st.tabs(
    [
        "📍 Estações SACE & Monitoramento",
        "🗺️ Manchas de Inundação (Satélite)",
        "💧 Previsão Hidrológica (Chuva x Nível)",
    ]
)

with tab_mapa:
    c_search, c_filter = st.columns([1, 2])
    with c_search:
        cidade_busca_mapa = st.selectbox(
            "🔍 Buscar Cidade do RS para Navegar:",
            options=["-- Selecionar Cidade --"] + sorted(MUNICIPIOS_RS_COORDS),
            key="search_mapa_tab1",
        )
    with c_filter:
        opcoes_bacia = sorted(df_estacoes["bacia"].dropna().unique())
        bacias_sel = st.multiselect(
            "Filtrar por Bacia Hidrográfica:",
            options=opcoes_bacia, default=opcoes_bacia,
        )

    df_filtrado = df_estacoes[df_estacoes["bacia"].isin(bacias_sel)].copy()

    center_pos = MUNICIPIOS_RS_COORDS.get(cidade_busca_mapa, [-29.7, -51.8])
    zoom_pos = 12 if cidade_busca_mapa != "-- Selecionar Cidade --" else 7

    col_map, col_tab = st.columns([2.2, 1.1])

    with col_map:
        mapa = folium.Map(location=center_pos, zoom_start=zoom_pos, tiles="CartoDB positron")
        for _, row in df_filtrado.dropna(subset=["lat", "lon"]).iterrows():
            folium.CircleMarker(
                location=[row["lat"], row["lon"]],
                radius=7 if row["tipo"] == "FLUVIOMETRICA" else 5,
                color=row["cor"], fill=True, fill_color=row["cor"],
                fill_opacity=0.9, weight=1,
                tooltip=(
                    f"<b>{row['nome']}</b><br>{row['situacao']}<br>"
                    f"Nível: {texto(row.get('nivel_cm'), ' cm')}<br>"
                    f"Chuva 24h: {texto(row.get('chuva_24h'), ' mm', 1)}"
                ),
            ).add_to(mapa)

        st_data = st_folium(mapa, width="100%", height=540, key="mapa_estacoes_sace")

        if st_data and st_data.get("last_object_clicked"):
            clique = st_data["last_object_clicked"]
            validas = df_filtrado.dropna(subset=["lat", "lon"]).copy()
            if not validas.empty:
                validas["dist"] = np.hypot(
                    validas["lat"] - clique["lat"], validas["lon"] - clique["lng"]
                )
                estacao_clicada = validas.nsmallest(1, "dist").iloc[0]
                if estacao_clicada["dist"] < 0.02:
                    exibir_boletim_modal(estacao_clicada)

    with col_tab:
        st.subheader("📊 Cotas do Rio")
        fluvio = df_filtrado[df_filtrado["tipo"] == "FLUVIOMETRICA"]
        st.dataframe(
            fluvio[["rio", "nome", "nivel_cm", "cota_alerta_cm", "cota_inundacao_cm"]]
            .sort_values("nome"),
            column_config={
                "rio": "Rio", "nome": "Estação",
                "nivel_cm": st.column_config.NumberColumn("Nível (cm)", format="%d"),
                "cota_alerta_cm": st.column_config.NumberColumn("Alerta", format="%d"),
                "cota_inundacao_cm": st.column_config.NumberColumn("Inundação", format="%d"),
            },
            hide_index=True, width='stretch', height=250,
        )

        st.subheader("🌧️ Acumulados de Chuva")
        com_chuva = df_filtrado[df_filtrado["chuva_24h"].notna()]
        st.dataframe(
            com_chuva[["nome", "chuva_1h", "chuva_24h", "chuva_72h"]]
            .sort_values("chuva_24h", ascending=False),
            column_config={
                "nome": "Estação",
                "chuva_1h": st.column_config.NumberColumn("1h", format="%.1f"),
                "chuva_24h": st.column_config.NumberColumn("24h", format="%.1f"),
                "chuva_72h": st.column_config.NumberColumn("72h", format="%.1f"),
            },
            hide_index=True, width='stretch', height=230,
        )

with tab_manchas:
    st.subheader("🗺️ Manchas de Inundação (Visualização de Satélite)")
    st.caption(
        "As manchas OFICIAIS vêm do geoportal do SGB (modelagem hidráulica do "
        "IPH-UFRGS, indexada por cota) e da Defesa Civil do RS (evento de "
        f"{gg.EVENTO_DEFESA_CIVIL}). Para cada estação, a mancha exibida é a "
        "maior cota mapeada que não passa do nível medido agora."
    )

    c_p1, c_p2 = st.columns([1, 3])

    with c_p1:
        st.markdown("#### 🔍 Localização & Seleção")
        cidade_busca_mancha = st.selectbox(
            "Ir para Cidade do RS:",
            options=["-- Selecionar Cidade --"] + sorted(MUNICIPIOS_RS_COORDS),
            key="search_mapa_tab2",
        )

        st.markdown("---")
        st.markdown("#### 🗺️ Manchas oficiais")
        ver_oficial_cota = st.checkbox(
            "🔴 Mancha por cota (SGB/IPH-UFRGS)", value=True,
            help="Modelagem hidráulica oficial, escolhida pela cota medida agora.",
        )
        ver_oficial_evento = st.checkbox(
            "🔵 Mancha do evento (Defesa Civil RS)", value=True,
            help=f"Área efetivamente alagada em {gg.EVENTO_DEFESA_CIVIL}.",
        )

        st.markdown("---")
        st.markdown("#### 🎨 Níveis de risco projetados")
        st.caption(
            "Cada camada é a área que a água alcança SE o rio subir até aquele "
            "limiar oficial. São aninhadas: inundação contém alerta, que contém "
            "atenção."
        )
        ver_atencao = st.checkbox("🟡 Cota de Atenção", value=True)
        ver_alerta = st.checkbox("🟠 Cota de Alerta", value=True)
        ver_inundacao = st.checkbox("🔴 Cota de Inundação", value=True)
        ver_atual = st.checkbox(
            "🔵 Contorno do nível ATUAL", value=True,
            help="Traço tracejado sobre a mancha correspondente ao nível medido agora.",
        )
        ver_eixo_rio = st.checkbox("🟢 Eixo do Rio (esquemático)", value=False)

        st.markdown("---")
        # LIMIARES PRIMEIRO, de propósito.
        #
        # O envelope roda o modelo hidrológico por estação para obter a
        # projeção — 30 s contra 5 s dos limiares. E como o Streamlit executa
        # o script inteiro a cada interação, essa espera bloqueia TODAS as
        # abas, inclusive a de Previsão Hidrológica, que aparecia em branco
        # enquanto o mapa calculava.
        #
        # Os limiares usam as cotas oficiais, que são fixas e já estão no
        # banco: nada a calcular. O envelope fica a um clique, para quem
        # quiser a projeção e aceitar a espera.
        modo_faixa = st.radio(
            "Faixas sobre o rio real:",
            ["Limiares fixos (atenção/alerta/inundação)", "Envelope da projeção"],
            help="Limiares fixos: as três cotas oficiais, imediato. "
                 "Envelope da projeção: a mancha da cota projetada com as cores "
                 "marcando a incerteza do modelo — mais informativo, porém leva "
                 "cerca de 30 s para calcular.",
        )
        ver_faixas = st.checkbox(
            "🌊 Exibir faixas (onde não há mancha oficial)", value=True,
            help="Traçado da hidrografia oficial do IBGE (1:100.000) com largura "
                 "estimada pela cota. NÃO é mancha modelada — cobre as estações "
                 "que o SGB não mapeou, como Estrela, Encantado e Muçum.",
        )

        ver_esquematico = st.checkbox(
            "Exibir desenho esquemático", value=False,
            help="Senos e cossenos ao redor da estação. NÃO é mancha medida — "
                 "serve só onde não há modelagem oficial.",
        )

        st.markdown("---")
        opac_val = st.slider("Opacidade da Mancha (%):", 20, 100, 60) / 100.0
        so_criticas = st.checkbox("Somente estações em risco", value=True)

    with c_p2:
        center_m2 = MUNICIPIOS_RS_COORDS.get(cidade_busca_mancha, [-29.6833, -51.4667])
        zoom_m2 = 13 if cidade_busca_mancha != "-- Selecionar Cidade --" else 8

        mapa_sat = folium.Map(
            location=center_m2, zoom_start=zoom_m2,
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Esri World Imagery",
        )
        folium.TileLayer(
            tiles="https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png",
            attr="CartoDB Labels", overlay=True,
        ).add_to(mapa_sat)

        alvos = df_estacoes[
            (df_estacoes["tipo"] == "FLUVIOMETRICA")
        ].dropna(subset=["lat", "lon"])
        if so_criticas:
            alvos = alvos[alvos["cor"].isin(["gold", "orange", "red", "purple"])]

        _camadas = tuple(
            c for c, ligado in (
                ("atencao", ver_atencao), ("alerta", ver_alerta),
                ("inundacao", ver_inundacao), ("atual", ver_atual),
            ) if ligado
        )
        resumo_oficial = gm.desenhar_manchas_oficiais(
            mapa_sat, alvos, opac_val, ver_oficial_cota, ver_oficial_evento,
            camadas=_camadas, mostrar_faixas=ver_faixas,
            modo_faixa="envelope" if modo_faixa.startswith("Envelope") else "limiares",
        )

        for _, est in (alvos.iterrows() if ver_esquematico else iter([])):
            rio = ou(est.get("rio"), est["nome"])
            poly_at, eixo = gm.gerar_morfologia_rio_e_mancha(est["lat"], est["lon"], 1.8)
            poly_al, _ = gm.gerar_morfologia_rio_e_mancha(est["lat"], est["lon"], 1.3)
            poly_in, _ = gm.gerar_morfologia_rio_e_mancha(est["lat"], est["lon"], 0.8)

            if ver_atencao:
                folium.Polygon(
                    locations=poly_at, color="#fbc02d", fill_color="#ffeb3b",
                    fill_opacity=opac_val, weight=1,
                    tooltip=f"Várzea de Atenção — {rio}",
                ).add_to(mapa_sat)
            if ver_alerta:
                folium.Polygon(
                    locations=poly_al, color="#e65100", fill_color="#ff9800",
                    fill_opacity=opac_val, weight=1,
                    tooltip=f"Área de Alerta — {rio}",
                ).add_to(mapa_sat)
            if ver_inundacao:
                folium.Polygon(
                    locations=poly_in, color="#b71c1c", fill_color="#f44336",
                    fill_opacity=opac_val, weight=1.5,
                    tooltip=f"Inundação Severa — {rio}",
                ).add_to(mapa_sat)
            if ver_eixo_rio:
                folium.PolyLine(
                    locations=eixo, color="#00e676", weight=2.5,
                    dash_array="6, 6", opacity=0.9, tooltip=f"Eixo do Rio: {rio}",
                ).add_to(mapa_sat)

        st_folium(mapa_sat, width="100%", height=580, key="mapa_manchas_satelite")

        n_cota = len(resumo_oficial["por_cota"])
        n_evento = len(resumo_oficial["por_evento"])
        if n_cota or n_evento:
            partes = []
            if n_cota:
                partes.append(f"**{n_cota}** mancha(s) oficial(is) por cota: "
                              + ", ".join(resumo_oficial["por_cota"]))
            if n_evento:
                partes.append(f"**{n_evento}** mancha(s) de evento: "
                              + ", ".join(resumo_oficial["por_evento"]))
            st.success(" · ".join(partes), icon="🗺️")
        else:
            st.info(
                "Nenhuma estação selecionada tem mancha oficial no nível atual. "
                "Existem manchas para Lajeado, São Sebastião do Caí, Montenegro, "
                "Alegrete e Uruguaiana (por cota) e para 7 municípios do Vale do "
                "Taquari (evento). Se o rio estiver abaixo da menor cota mapeada, "
                "não há o que desenhar."
            )
        st.caption(
            f"{len(alvos)} estação(ões) no recorte · "
            f"{len(resumo_oficial['sem_mancha'])} sem mancha oficial."
        )

# -----------------------------------------------------------------------------
# 7. PREVISÃO HIDROLÓGICA — módulo georisk_hidrologia
# -----------------------------------------------------------------------------
with tab_hidro:
    st.subheader("💧 Relação Chuva x Nível, tempo de resposta e projeção")
    st.caption(
        "Quanto choveu sobre a área contribuinte, quanto disso escoa segundo o "
        "solo e o uso da terra da bacia, quanto o rio sobe por causa disso e em "
        "quanto tempo. O cruzamento das cotas oficiais do SACE é consequência, "
        "não o objetivo da conta — e as estações sem cota publicada continuam "
        "tendo projeção de quanto e quando."
    )

    @st.cache_data(ttl=300)
    def _analisaveis() -> pd.DataFrame:
        return gh.estacoes_analisaveis()

    @st.cache_data(ttl=300, show_spinner="Modelando resposta da bacia…")
    def _analisar(estacao_id: str) -> dict:
        """Cacheia só os DADOS, nunca a figura.

        Guardar o objeto Figure do Plotly no cache fazia o Streamlit reusar o
        mesmo nó entre reruns e o React quebrava com
        `NotFoundError: Failed to execute 'removeChild' on 'Node'`. A figura é
        barata de remontar; a modelagem é que é cara.
        """
        # cn_base=None faz o módulo buscar o CN REAL da bacia (pedologia e uso
        # da terra do IBGE, refinados pela litologia). O painel tinha um slider
        # que sobrescrevia esse valor com um chute do usuário — Passo Carreiro
        # analisado com CN 52 quando a bacia dele tem outro. Agora o dado manda.
        resultado = gh.estimar_tempo_e_impacto_inundacao(estacao_id, cn_base=None)
        resultado.pop("grafico_hietograma_hidrograma", None)
        return resultado

    def _projecao_series(resultado: dict):
        """Reconstrói a série de projeção a partir do dicionário cacheado."""
        pontos = resultado.get("projecao") or []
        if not pontos:
            return None
        return pd.Series(
            [p["cota_projetada_cm"] for p in pontos],
            index=pd.DatetimeIndex([p["instante"] for p in pontos]),
        )

    # --- SÓ O QUE SUSTENTA DECISÃO
    #
    # Esta aba passa a listar apenas estações cuja projeção bateu a persistência
    # em previsões passadas MEDIDAS. Não é filtro estético: exibir junto o que
    # não tem lastro obriga quem decide a separar o joio no meio de uma
    # emergência, e a separação já foi feita aqui com número.
    #
    # A barra sobe sozinha: `--calibrar` roda a cada coleta, cada rodada
    # acrescenta um snapshot ao histórico, e estação que passa a acertar entra
    # na lista sem ninguém mexer em nada.
    _todas_analisaveis = _analisaveis()
    _cache_selo = gh.carregar_projecoes()
    if not _cache_selo.empty and "confiavel" in _cache_selo.columns:
        aptos = set(
            _cache_selo.loc[_cache_selo["confiavel"] == 1, "id_estacao"]
        )
        elegiveis = _todas_analisaveis[_todas_analisaveis["id"].isin(aptos)]
    else:
        elegiveis = _todas_analisaveis
    ocultas = len(_todas_analisaveis) - len(elegiveis)

    if elegiveis.empty and not _todas_analisaveis.empty:
        st.warning(
            f"**Nenhuma das {len(_todas_analisaveis)} estações atinge a barra de "
            f"decisão neste momento.** O critério é ter batido a persistência em "
            f"previsões passadas medidas. Rode `python georisk_hidrologia.py "
            f"--calibrar` e depois `--projetar` para reavaliar com o histórico "
            f"mais recente.",
            icon="⚠️",
        )
    elif elegiveis.empty:
        st.warning(
            "Nenhuma estação tem as duas séries (cota e chuva) no banco. Rode uma "
            "coleta incluindo o SACE — é ele que publica a série de 15 em 15 min."
        )
    else:
        def _rotulo(r) -> str:
            """Rótulo completo da estação.

            `ou()` e não `r.rio or ...`: rio vazio chega do SQLite como NaN, que
            é verdadeiro em Python — o painel imprimia "Passo Tainhas — nan".
            """
            chuva = (
                "sem chuva no período" if pd.isna(r.chuva_mm_periodo)
                else f"{r.chuva_mm_periodo:.0f} mm no período"
            )
            return f"{r.nome} — {ou(r.rio, 'rio não informado')} ({chuva})"

        rotulos = {_rotulo(r): r.id for r in elegiveis.itertuples()}

        # Fonte única da seleção: o rótulo escolhido. Mapa e lista escrevem
        # aqui; tudo abaixo lê daqui. Revalidado porque a lista de estações
        # elegíveis muda entre coletas e o rótulo guardado pode sumir.
        if st.session_state.get("hidro_estacao") not in rotulos:
            st.session_state["hidro_estacao"] = next(iter(rotulos))

        def _selecionar(rotulo: str) -> None:
            if rotulo != st.session_state.get("hidro_estacao"):
                st.session_state["hidro_estacao"] = rotulo
                st.rerun()

        estacao_id = rotulos[st.session_state["hidro_estacao"]]
        atual = elegiveis[elegiveis["id"] == estacao_id].iloc[0]

        def _cor_agora(linha) -> tuple[str, str]:
            """Cor pela leitura ATUAL contra as cotas oficiais."""
            nivel = linha.get("nivel_cm")
            if nivel is None or pd.isna(nivel):
                return "#9e9e9e", "sem leitura atual"
            for campo, cor, nome in (
                ("cota_inundacao_cm", "#f44336", "acima da inundação"),
                ("cota_alerta_cm", "#ff9800", "acima do alerta"),
                ("cota_atencao_cm", "#ffeb3b", "acima da atenção"),
            ):
                limiar = linha.get(campo)
                if limiar is not None and not pd.isna(limiar) and nivel >= limiar:
                    return cor, nome
            return "#4caf50", "abaixo dos limiares"

        # Escada única de cores para as duas réguas da projeção: a oficial
        # (onde o SACE publica cota) e a amplitude recente (nas demais). A cor
        # diz a gravidade; o balão diz por qual régua ela foi obtida.
        CORES_PROJECAO = {
            "inundacao": ("#f44336", "vai atingir a inundação"),
            "subida_forte": ("#f44336", "subida forte"),
            "alerta": ("#ff9800", "vai atingir o alerta"),
            "subida_moderada": ("#ff9800", "subida moderada"),
            "atencao": ("#ffeb3b", "vai atingir a atenção"),
            "subida_leve": ("#ffeb3b", "subida leve"),
            "abaixo": ("#4caf50", "abaixo das cotas oficiais"),
            "estavel": ("#4caf50", "estável"),
            "indefinida": ("#9e9e9e", "sem projeção"),
        }

        @st.cache_data(ttl=300)
        def _projecoes() -> pd.DataFrame:
            return gh.carregar_projecoes()

        cache_proj = _projecoes()
        tem_cache = not cache_proj.empty
        por_estacao = (
            cache_proj.set_index("id_estacao").to_dict("index") if tem_cache else {}
        )

        col_modo, col_idade = st.columns([1, 2])
        with col_modo:
            modo_cor = st.radio(
                "Colorir marcadores por:", ["Projeção", "Leitura atual"],
                horizontal=True, key="hidro_modo_cor",
                disabled=not tem_cache,
                help="A projeção vem de um cache calculado fora da interação — "
                     "projetar as 59 estações leva ~1,2 min e travaria o painel.",
            )
        with col_idade:
            if tem_cache:
                calculado = cache_proj["calculado_em"].max()
                idade = (pd.Timestamp.now() - pd.Timestamp(calculado)).total_seconds() / 60
                aviso = "⚠️ desatualizada" if idade > 240 else "atualizada"
                st.caption(
                    f"Projeção {aviso} — calculada em {calculado} "
                    f"({idade:.0f} min atrás). Recalcule com "
                    "`python georisk_hidrologia.py --projetar`."
                )
            else:
                modo_cor = "Leitura atual"
                st.caption(
                    "Projeção ainda não calculada — marcadores pela leitura atual. "
                    "Rode `python georisk_hidrologia.py --projetar` para gerar."
                )

        # Visão sistêmica: quantas estações têm a série parada. Sem isto, só se
        # descobre abrindo estação por estação — e foi assim que uma pane de
        # quatro dias no CSV do SACE passou despercebida.
        if tem_cache and "confiavel" in cache_proj.columns:
            n_conf = int(cache_proj["confiavel"].fillna(0).sum())
            if n_conf == 0:
                st.warning(
                    f"**Nenhuma das {len(cache_proj)} estações tem projeção "
                    f"confiável agora.** A causa principal é a série de 15 min: "
                    f"o SACE parou de publicá-la, e sem dado novo a projeção "
                    f"parte de onde a série terminou. Abra uma estação para ver "
                    f"há quanto tempo a dela parou.",
                    icon="⚠️",
                )

        def _cor_da_estacao(linha) -> tuple[str, str]:
            if modo_cor == "Leitura atual":
                return _cor_agora(linha)
            reg = por_estacao.get(linha["id"])
            if not reg:
                return "#9e9e9e", "sem projeção no cache"
            cor, _ = CORES_PROJECAO.get(reg["classe"], CORES_PROJECAO["indefinida"])
            return cor, reg["motivo"]

        def _linha_de_nivel(nivel_cadastro, reg: dict) -> str:
            """Nível e pico no balão, sem misturar procedência.

            `nivel_cm` é a leitura pontual do cadastro; `cota_atual_cm` do cache
            é o fim da série, que é de onde a projeção parte. Onde só existe a
            segunda, é ela que aparece — rotulada — em vez de "Sem dado" ao lado
            de uma variação que ficaria sem referência.
            """
            pico = (reg or {}).get("pico_projetado_cm")
            ancora = (reg or {}).get("cota_atual_cm")
            tem_pico = pico is not None and not pd.isna(pico)

            if nivel_cadastro is not None and not pd.isna(nivel_cadastro):
                base, rotulo = float(nivel_cadastro), "Nível"
            elif ancora is not None and not pd.isna(ancora):
                base, rotulo = float(ancora), "Nível (fim da série)"
            else:
                return "Nível: Sem dado" + (
                    f" · pico projetado {texto(pico, ' cm')}" if tem_pico else ""
                )

            texto_base = f"{rotulo}: {base:.0f} cm"
            if not tem_pico:
                return texto_base
            return f"{texto_base} → pico {float(pico):.0f} cm ({float(pico) - base:+.0f} cm)"

        col_mapa, col_lista = st.columns([2.4, 1])

        with col_mapa:
            centro = (
                [atual["lat"], atual["lon"]]
                if not pd.isna(atual["lat"]) and not pd.isna(atual["lon"])
                else [-29.7, -51.8]
            )
            zoom_h = 9 if centro != [-29.7, -51.8] else 7

            mapa_h = folium.Map(
                location=centro, zoom_start=zoom_h,
                tiles="https://server.arcgisonline.com/ArcGIS/rest/services/"
                      "World_Imagery/MapServer/tile/{z}/{y}/{x}",
                attr="Esri World Imagery",
            )
            folium.TileLayer(
                tiles="https://{s}.basemaps.cartocdn.com/rastertiles/"
                      "voyager_only_labels/{z}/{x}/{y}{r}.png",
                attr="CartoDB Labels", overlay=True,
            ).add_to(mapa_h)

            for _, linha in elegiveis.dropna(subset=["lat", "lon"]).iterrows():
                cor, estado = _cor_da_estacao(linha)
                ativa = linha["id"] == estacao_id
                reg = por_estacao.get(linha["id"]) or {}
                pico = reg.get("pico_projetado_cm")
                variacao = reg.get("variacao_cm")
                folium.CircleMarker(
                    location=[linha["lat"], linha["lon"]],
                    # Pequenas de propósito: 59 marcadores grandes viravam
                    # mancha contínua no zoom estadual e escondiam o terreno.
                    radius=5 if ativa else 2.5,
                    color="#ffffff" if ativa else cor,
                    weight=2 if ativa else 0.5,
                    fill=True, fill_color=cor, fill_opacity=0.95,
                    tooltip=(
                        f"<b>{linha['nome']}</b><br>"
                        f"{ou(linha.get('municipio'), 'município não informado')} · "
                        f"{ou(linha.get('rio'), 'rio não informado')}<br>"
                        # O nível vem do cadastro e o pico vem da série, e as
                        # duas fontes divergem: estação sem leitura pontual
                        # mostrava "Sem dado → pico 471 cm (+207 cm)", uma
                        # variação a partir de um nível desconhecido. Quando o
                        # cadastro não tem leitura, a âncora exibida passa a ser
                        # a mesma que a projeção usou, e dita como tal.
                        + _linha_de_nivel(linha.get("nivel_cm"), reg)
                        + f"<br><b>{estado}</b><br>"
                        f"Atenção: {texto(linha.get('cota_atencao_cm'), ' cm')} · "
                        f"Alerta: {texto(linha.get('cota_alerta_cm'), ' cm')} · "
                        f"Inundação: {texto(linha.get('cota_inundacao_cm'), ' cm')}"
                        "<br><i>clique para analisar</i>"
                    ),
                ).add_to(mapa_h)

            clique_h = st_folium(
                mapa_h, width="100%", height=470, key="mapa_hidro",
                returned_objects=["last_object_clicked"],
            )

            # O clique volta como coordenada, não como id — casamos pela
            # estação mais próxima, com tolerância de ~2 km.
            alvo = (clique_h or {}).get("last_object_clicked")
            if alvo:
                com_pos = elegiveis.dropna(subset=["lat", "lon"]).copy()
                com_pos["dist"] = np.hypot(
                    com_pos["lat"] - alvo["lat"], com_pos["lon"] - alvo["lng"]
                )
                perto = com_pos.nsmallest(1, "dist").iloc[0]
                if perto["dist"] < 0.02:
                    _selecionar(_rotulo(perto))

        with col_lista:
            st.markdown("**Estações analisáveis**")
            busca_h = st.text_input(
                "Filtrar", placeholder="filtrar por cidade ou rio…",
                key="hidro_busca", label_visibility="collapsed",
            )
            alvo_busca = (busca_h or "").strip().lower()
            visiveis = [r for r in rotulos if alvo_busca in r.lower()]

            if not visiveis:
                st.caption("Nenhuma estação com esse termo.")
            with st.container(height=390):
                for rotulo in visiveis:
                    linha = elegiveis[elegiveis["id"] == rotulos[rotulo]].iloc[0]
                    cor, _ = _cor_da_estacao(linha)
                    cidade = ou(linha.get("municipio"), linha["nome"])
                    # A lista repete a cor do mapa — quem varre a lista vê a
                    # mesma gravidade sem ter que procurar o ponto no terreno.
                    bolinha = {
                        "#f44336": "🔴", "#ff9800": "🟠", "#ffeb3b": "🟡",
                        "#4caf50": "🟢",
                    }.get(cor, "⚪")
                    if st.button(
                        f"{bolinha} {cidade} — {texto(linha.get('nivel_cm'), ' cm')}",
                        key=f"hidro_pick_{rotulos[rotulo]}",
                        width="stretch",
                        type="primary" if rotulos[rotulo] == estacao_id else "secondary",
                    ):
                        _selecionar(rotulo)

            st.caption(
                f"{len(visiveis)} de {len(rotulos)} · "
                + ("cor pela projeção — o balão diz por qual régua"
                   if modo_cor == "Projeção" else
                   "cor pela leitura atual contra as cotas oficiais")
                + (f" · **{ocultas} ocultas** por ainda não baterem a "
                   "persistência em previsões medidas" if ocultas else "")
            )

        st.markdown(f"**Analisando:** {st.session_state['hidro_estacao']}")

        resultado = _analisar(estacao_id)

        # --- A CADEIA: choveu -> escoou -> sobe -> em quanto tempo
        #
        # O cabeçalho mostrava "Até a Cota de Inundação", o que fazia a cota
        # oficial parecer o objetivo da conta. Não é: 33 das 59 estações não
        # têm cota publicada e continuam tendo projeção. O que o modelo produz
        # é quanto sobe e quando, a partir do que choveu sobre a bacia e das
        # características dela. O cruzamento de limiar é leitura derivada, e
        # está logo abaixo, na tabela de cotas oficiais.
        acum = resultado.get("precipitacao_acumulada_mm") or {}
        balanco = resultado.get("balanco_hidrico") or {}
        aferic = resultado.get("afericao_tc")
        atual_cm = resultado["cota_atual_cm"]
        pico_cm = resultado["cota_maxima_projetada_cm"]

        # Âncora velha primeiro: sem isso, todo número abaixo parece de agora.
        if resultado.get("ancora_defasada"):
            st.error(
                f"**Esta projeção não é de agora.** A série de 15 min desta "
                f"estação parou em **{resultado['ancora_em']}**, há "
                f"**{resultado['idade_ancora_horas']:.0f} h**, e é dela que a "
                f"projeção parte — os instantes abaixo são contados a partir "
                f"daquele momento, não deste. A fonte segue publicando a "
                f"leitura pontual, mas parou de publicar a série; é limitação "
                f"do SACE, não do cálculo.",
                icon="🕐",
            )

        k1, k2, k3, k4 = st.columns(4)

        # A janela do passo 1 tem que ser a MESMA que o balanço consome, senão a
        # cadeia mente por composição: Porto Mauá exibia "choveu 129 mm" (72 h)
        # ao lado de "escoou 0 m³", quando o balanço tinha sido feito sobre os
        # 3 mm das últimas 24 h — e zero escoamento para 3 mm está correto.
        chuva_do_balanco = balanco.get("precipitacao_total_mm")
        k1.metric("1 · Choveu (24 h)", texto(chuva_do_balanco, " mm", 1))
        k1.caption(
            f"{ou(resultado.get('origem_chuva'), 'chuva da própria estação')} · "
            f"{texto(acum.get('72h'), ' mm', 1)} em 72 h"
        )

        volume = balanco.get("volume_escoado_m3")
        k2.metric(
            "2 · Escoou",
            "Sem dado" if volume is None or pd.isna(volume)
            else (f"{volume / 1e6:.1f} milhões m³" if volume >= 1e6 else f"{volume:,.0f} m³"),
        )
        k2.caption(
            f"CN {texto(resultado.get('cn_base'), '', 1)} · "
            f"escoa {texto((balanco.get('coeficiente_escoamento') or 0) * 100, ' %', 0)} "
            f"do que cai · solo em "
            f"{texto((resultado.get('vulnerabilidade') or {}).get('saturacao_pct'), ' %', 0)}"
        )

        k3.metric(
            "3 · Sobe até",
            texto(pico_cm, " cm"),
            delta=(
                None if pico_cm is None or atual_cm is None
                else f"{pico_cm - atual_cm:+.0f} cm sobre os {atual_cm:.0f} de agora"
            ),
        )
        k3.caption(f"tendência: {ou(resultado.get('tendencia'))}")

        k4.metric(
            "4 · Em",
            f"{resultado['tc_horas']:.1f} h" if resultado["tc_horas"] else "Indeterminado",
            help="Tempo de resposta da bacia: defasagem de maior correlação "
                 "entre a chuva e a taxa de subida do rio."
                 + (f" Aferição pela densidade de drenagem ({aferic['densidade_drenagem']}): "
                    f"esperado {aferic['tc_esperado_h'][0]:.0f}–{aferic['tc_esperado_h'][1]:.0f} h — "
                    f"{aferic['veredito']}." if aferic else ""),
        )
        pico_em = resultado.get("instante_pico_projetado")
        k4.caption(
            f"pico em {pico_em}" if pico_em else "sem instante de pico projetado"
        )

        # --- O que está empurrando o rio
        #
        # O balanço SCS-CN e a projeção são contas DIFERENTES e podem discordar:
        # o SCS-CN é físico e só modela escoamento superficial da chuva nova; a
        # projeção é estatística e usa nível, taxa de subida, cinco janelas de
        # chuva e as características da bacia (CN, densidade de drenagem,
        # lineamentos, área). Quando o balanço dá zero e o rio sobe assim mesmo,
        # a diferença não é erro — é água que já está no sistema, e a tela deve
        # dizer isso em vez de deixar parecer contradição.
        sobe = (
            pico_cm is not None and atual_cm is not None and pico_cm - atual_cm > 1
        )
        escoamento_nulo = not (balanco.get("volume_escoado_m3") or 0) > 0
        montantes = resultado.get("montante") or []

        if sobe and escoamento_nulo:
            st.info(
                f"**A chuva recente não gera escoamento superficial agora** — "
                f"{texto(balanco.get('precipitacao_total_mm'), ' mm', 1)} não "
                f"superam a abstração inicial de "
                f"{texto(balanco.get('abstracao_inicial_mm'), ' mm', 1)} "
                f"(solo em {texto(balanco.get('saturacao_pct'), ' %', 0)} de "
                f"saturação). A subida projetada vem da água **já em trânsito**: "
                f"o modelo lê o nível e a taxa de subida atuais junto com as "
                f"janelas de chuva e as características da bacia. O balanço "
                f"SCS-CN mede quanto a chuva NOVA acrescenta; a projeção mede "
                f"para onde o rio vai.",
                icon="💡",
            )

        st.caption(
            "Entram na projeção: nível e taxa de subida atuais, chuva de 1 h, "
            "3 h, 12 h, 24 h e 72 h, Curve Number da bacia, densidade de "
            "drenagem, densidade de lineamentos e área — "
            f"via {ou(resultado.get('origem_projecao'))}."
            + (" Montante considerado: "
               + ", ".join(f"{m['nome']} (+{m['lag_horas']:.1f} h, r={m['correlacao']:.2f})"
                           for m in montantes)
               + ("." if resultado.get("usou_montante")
                  else " — testado e NÃO usado, não melhorou a previsão.")
               if montantes else "")
        )

        # --- Confiabilidade em destaque: é o que decide se dá para usar
        # O selo agora carrega o número medido. "Confiável" sozinho é opinião;
        # "errou 52 cm em 55 previsões passadas" é verificável — e é o que
        # permite decidir se a margem cabe na decisão que se vai tomar.
        medido = resultado.get("desempenho_medido")
        if resultado["confiavel"] and medido:
            st.success(
                f"**Erro medido de {medido['mae_cm']:.0f} cm** neste horizonte "
                f"({medido['faixa']}), em {medido['n']} previsões passadas desta "
                f"{medido['origem']} — contra "
                f"{medido['mae_persistencia_cm']:.0f} cm de supor o nível parado. "
                f"Tendência: {resultado['tendencia']}.",
                icon="✅",
            )
            if abs(medido.get("vies_cm") or 0) > 20:
                lado = "SUBESTIMA" if medido["vies_cm"] < 0 else "superestima"
                st.caption(
                    f"Viés de {medido['vies_cm']:+.0f} cm: historicamente esta "
                    f"faixa **{lado}** o pico. Considere na margem."
                )
        elif resultado["confiavel"]:
            st.success(
                f"Projeção com ganho real sobre a persistência até "
                f"**{resultado['horizonte_util_horas']:.1f} h** à frente "
                f"(tendência: {resultado['tendencia']}).",
                icon="✅",
            )
        else:
            st.warning(
                "**Projeção sem confiabilidade estatística no momento.** Os números "
                "abaixo servem para diagnóstico, não para decisão.",
                icon="⚠️",
            )
        for aviso in resultado["avisos"]:
            st.caption(f"• {aviso}")

        # --- Gráfico hietograma x hidrograma (remontado fora do cache)
        _serie_est = gh.carregar_series_alinhadas(estacao_id)
        figura = gh.grafico_hietograma_hidrograma(
            _serie_est,
            gh.carregar_cadastro(estacao_id),
            _projecao_series(resultado),
            resultado["tc_horas"],
        )
        st.plotly_chart(figura, width='stretch', key=f"hidro_{estacao_id}")

        # --- Balanço volumétrico e detalhamento
        col_bal, col_proj = st.columns(2)

        with col_bal:
            st.markdown("##### 🌧️ Balanço volumétrico (SCS-CN)")
            balanco = resultado["balanco_hidrico"] or {}
            st.dataframe(
                pd.DataFrame(
                    {
                        "Grandeza": [
                            "Precipitação 24 h",
                            "Precipitação efetiva (escoamento)",
                            "Abstração inicial",
                            "Retenção potencial (S)",
                            "CN ajustado",
                            "Umidade do solo (pelas 72 h)",
                        ],
                        "Valor": [
                            f"{balanco.get('precipitacao_total_mm', 0):.1f} mm",
                            f"{balanco.get('precipitacao_efetiva_mm', 0):.1f} mm",
                            f"{balanco.get('abstracao_inicial_mm', 0):.1f} mm",
                            f"{balanco.get('retencao_potencial_mm', 0):.1f} mm",
                            f"{balanco.get('cn_ajustado', 0):.1f}",
                            balanco.get("condicao_umidade", "—"),
                        ],
                    }
                ),
                hide_index=True, width='stretch',
            )
            st.markdown("##### Precipitação acumulada")
            st.dataframe(
                pd.DataFrame(
                    list(resultado["precipitacao_acumulada_mm"].items()),
                    columns=["Janela", "mm"],
                ),
                hide_index=True, width='stretch',
            )

        with col_proj:
            st.markdown("##### 📈 Projeção por horizonte")
            if resultado["projecao"]:
                proj = pd.DataFrame(resultado["projecao"])
                proj["util"] = proj["ganho_sobre_persistencia"].apply(
                    lambda g: "✅ útil" if (g or -1) > 0 else "⚠️ sem ganho"
                )
                st.dataframe(
                    proj[["horas_a_frente", "cota_projetada_cm", "util"]],
                    column_config={
                        "horas_a_frente": st.column_config.NumberColumn(
                            "h à frente", format="%.2f"
                        ),
                        "cota_projetada_cm": st.column_config.NumberColumn(
                            "Cota (cm)", format="%.0f"
                        ),
                        "util": "Ganho sobre persistência",
                    },
                    hide_index=True, width='stretch',
                )
            st.markdown("##### 🚦 Situação das cotas oficiais")
            # Repetir "não atingida" três vezes não informa nada. O que o
            # operador precisa saber é QUAL é o limiar, QUANTO falta para ele e
            # se a projeção chega lá — por isso a tabela traz os números.
            atual = resultado.get("cota_atual_cm")
            pico = resultado.get("cota_maxima_projetada_cm")
            linhas_limiar = []
            for rotulo, campo, chave_tempo in (
                ("🟡 Atenção", "cota_atencao_cm", "tempo_horas_ate_atencao"),
                ("🟠 Alerta", "cota_alerta_cm", "tempo_horas_ate_alerta"),
                ("🔴 Inundação", "cota_inundacao_cm", "tempo_horas_ate_inundacao"),
            ):
                limiar = resultado.get(campo)
                if limiar is None or pd.isna(limiar):
                    linhas_limiar.append(
                        {"Cota": rotulo, "Limiar (cm)": None, "Falta (cm)": None,
                         "Pico projetado alcança?": "sem cota oficial publicada",
                         "Prazo": "—"}
                    )
                    continue

                limiar = float(limiar)
                falta = None if atual is None else round(limiar - float(atual))
                horas = resultado.get(chave_tempo)

                if falta is not None and falta <= 0:
                    veredito, prazo = "JÁ ULTRAPASSADA", "agora"
                elif horas is not None:
                    veredito, prazo = "SIM", f"em ~{horas:.1f} h"
                elif pico is not None:
                    veredito = "não — falta {:.0f} cm no pico".format(limiar - float(pico))
                    prazo = "—"
                else:
                    veredito, prazo = "sem projeção", "—"

                linhas_limiar.append(
                    {"Cota": rotulo, "Limiar (cm)": limiar, "Falta (cm)": falta,
                     "Pico projetado alcança?": veredito, "Prazo": prazo}
                )

            st.dataframe(
                pd.DataFrame(linhas_limiar),
                column_config={
                    "Limiar (cm)": st.column_config.NumberColumn(format="%d"),
                    "Falta (cm)": st.column_config.NumberColumn(
                        format="%d", help="Quanto o rio ainda precisa subir."
                    ),
                },
                hide_index=True, width='stretch',
            )
            if atual is not None and pico is not None:
                st.caption(
                    f"Nível agora: **{atual:.0f} cm** · pico projetado: "
                    f"**{pico:.0f} cm** · tendência: {resultado.get('tendencia')}."
                )

        with st.expander("🔬 Diagnóstico do modelo"):
            st.json(
                {
                    "tc_horas": resultado["tc_horas"],
                    "metodo_tc": resultado["metodo_tc"],
                    "cn_base": resultado.get("cn_base"),
                    "cn_origem": resultado.get("cn_origem"),
                    "afericao_tc": resultado.get("afericao_tc"),
                    "correlacao_chuva_nivel": resultado["correlacao_chuva_nivel"],
                    "ganho_sobre_persistencia": resultado["ganho_sobre_persistencia"],
                    "horizonte_util_horas": resultado["horizonte_util_horas"],
                    "r2_validacao_walk_forward": resultado["qualidade_ajuste_r2"],
                    "r2_treino": resultado["qualidade_ajuste_r2_treino"],
                    "tendencia": resultado["tendencia"],
                    "pico_ja_ocorreu": resultado["pico_ja_ocorreu"],
                }
            )
            st.caption(
                "O R² de treino é sempre bem maior que o de validação — assinatura "
                "de um modelo que se ajusta ao passado recente. O número que vale "
                "para decidir é o ganho sobre a persistência."
            )


# -----------------------------------------------------------------------------
# 8. VIGIA DA ATUALIZAÇÃO AUTOMÁTICA
# -----------------------------------------------------------------------------
# Fica NO FIM de propósito. O Streamlit executa o script de cima para baixo, e
# a coleta leva ~2 min: chamando isto no topo, abrir o painel com dado velho
# deixaria a tela em branco até terminar. Aqui, a página aparece na hora com o
# que já existe, e a atualização acontece depois — o `st.rerun` redesenha tudo
# com o dado novo quando termina.
vigia()
