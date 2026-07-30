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

    incluir_ana = st.checkbox("Incluir telemetria da ANA (+500 estações)", value=False)
    auto = st.checkbox("Atualização automática", value=False)
    intervalo_min = st.number_input("A cada (min):", 5, 180, 15, step=5)

    if st.button("⬇️ Atualizar agora", use_container_width=True, type="primary"):
        atualizar(incluir_ana)

    st.divider()
    st.caption(
        "Cotas de atenção/alerta/inundação são as **oficiais do SACE**, lidas "
        "junto com o nível. Estação sem cota publicada aparece cinza — nunca "
        "arbitramos um limiar."
    )


@st.fragment(run_every="60s")
def vigia() -> None:
    if not auto:
        return
    atual = gd.idade_dados()
    if atual is None or atual > timedelta(minutes=int(intervalo_min)):
        with st.spinner("Atualização automática…"):
            gd.sincronizar(incluir_ana=incluir_ana)
        st.cache_data.clear()
        st.rerun(scope="app")


vigia()

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
def gerar_morfologia_rio_e_mancha(lat_centro, lon_centro, fator_largura=1.0):
    """Gera meandros esquemáticos e as manchas que acompanham a calha.
    Atenção: é desenho ilustrativo, não mancha de inundação medida."""
    t = np.linspace(0, 4 * np.pi, 70)
    lats_eixo = lat_centro + (t * 0.005) - 0.03
    lons_eixo = lon_centro + np.sin(t) * 0.012 + np.cos(t * 0.5) * 0.008
    larguras = (np.sin(t * 2) * 0.004 + 0.01) * fator_largura
    lado_esq_lat = lats_eixo + larguras
    lado_esq_lon = lons_eixo - larguras * 0.8
    lado_dir_lat = lats_eixo[::-1] - larguras[::-1]
    lado_dir_lon = lons_eixo[::-1] + larguras[::-1] * 0.8
    poly_lats = np.concatenate([lado_esq_lat, lado_dir_lat])
    poly_lons = np.concatenate([lado_esq_lon, lado_dir_lon])
    poligono_fechado = [[lat, lon] for lat, lon in zip(poly_lats, poly_lons)]
    eixo_rio = [[lat, lon] for lat, lon in zip(lats_eixo, lons_eixo)]
    return poligono_fechado, eixo_rio


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

    k1, k2, k3 = st.columns(3)
    k1.metric("Nível atual", texto(row.get("nivel_cm"), " cm"))
    k2.metric("Chuva 24h", texto(row.get("chuva_24h"), " mm", 1))
    k3.metric("Vazão", texto(row.get("vazao_m3s"), " m³/s", 1))

    # pd.notna: vindo do banco, campo vazio chega como NaN (que é "verdadeiro").
    if pd.notna(row.get("observacao")):
        st.warning(f"Valor descartado por implausibilidade física: {row['observacao']}")

    figura = gerar_grafico_estilo_sace(row)
    if figura is None:
        st.info("Ainda sem série histórica no banco para esta estação.")
    else:
        st.plotly_chart(figura, use_container_width=True)

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
            use_container_width=True, disabled=serie_cota.empty,
        )
    with c2:
        st.download_button(
            "📥 Dados Chuva (CSV)",
            data=(
                serie_chuva.to_csv(index=False).encode("utf-8")
                if not serie_chuva.empty else b"sem dado\n"
            ),
            file_name=f"chuva_{row['id']}.csv", mime="text/csv",
            use_container_width=True, disabled=serie_chuva.empty,
        )
    with c3:
        st.info(f"Vazão medida: **{texto(row.get('vazao_m3s'), ' m³/s', 1)}**")


# -----------------------------------------------------------------------------
# 6. ABAS
# -----------------------------------------------------------------------------
tab_mapa, tab_manchas = st.tabs(
    ["📍 Estações SACE & Monitoramento", "🗺️ Manchas de Inundação (Satélite)"]
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
            hide_index=True, use_container_width=True, height=250,
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
            hide_index=True, use_container_width=True, height=230,
        )

with tab_manchas:
    st.subheader("🗺️ Manchas de Inundação (Visualização de Satélite)")
    st.warning(
        "⚠️ O polígono é **esquemático** (gerado matematicamente ao redor da "
        "estação), não é a mancha de inundação oficial. O que é real é a cor: "
        "vem da cota medida comparada à cota oficial do SACE.",
        icon="⚠️",
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
        st.markdown("#### 🎨 Seleção de Camadas de Cotas")
        ver_atencao = st.checkbox("🟡 Camada - Cota de Atenção", value=True)
        ver_alerta = st.checkbox("🟠 Camada - Cota de Alerta", value=True)
        ver_inundacao = st.checkbox("🔴 Camada - Cota de Inundação", value=True)
        ver_eixo_rio = st.checkbox("🟢 Exibir Leito/Eixo do Rio (Traçado)", value=True)

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

        for _, est in alvos.iterrows():
            rio = ou(est.get("rio"), est["nome"])
            poly_at, eixo = gerar_morfologia_rio_e_mancha(est["lat"], est["lon"], 1.8)
            poly_al, _ = gerar_morfologia_rio_e_mancha(est["lat"], est["lon"], 1.3)
            poly_in, _ = gerar_morfologia_rio_e_mancha(est["lat"], est["lon"], 0.8)

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
        st.caption(f"{len(alvos)} estação(ões) desenhada(s), com cor de risco real.")
