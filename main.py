"""
GeoRisk-RS — Sala de Decisão SACE / Earth
==========================================
100% DADOS REAIS. Não existe mais nenhum catálogo fixo de níveis, chuvas ou
status: tudo é lido do banco `georisk_rs.db`, que por sua vez é alimentado
pelo coletor `georisk_dados.py` a partir das fontes oficiais publicadas em
HTML/CSV/XML:

  - SACE / SGB-CPRM  -> nível, COTAS OFICIAIS de atenção/alerta/inundação,
                        situação e série real de 15 em 15 min (30 dias);
  - ANA / Telemetria -> estações automáticas do RS (nível, vazão, chuva).

Regra do projeto: onde a fonte não publica, aparece "Sem dado" e o ponto fica
cinza. Nada é preenchido com valor sintético.

COMO RODAR
  1) pip install streamlit folium streamlit-folium plotly pandas requests
  2) streamlit run main.py
  3) Na primeira vez, clique em "Atualizar agora" na barra lateral (ou deixe a
     atualização automática ligada). O coletor leva ~40 s no modo rápido
     (só SACE) e ~2 min no modo completo (SACE + ANA).

Os três arquivos do projeto:
  georisk_dados.py  -> motor: coleta real + banco (é onde mexer na fonte)
  main.py           -> este painel
  dashboard.py      -> painel alternativo, mesmo banco
"""

from __future__ import annotations

import warnings
from datetime import datetime, timedelta

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
# 1. CONFIGURAÇÃO DE PÁGINA
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="GeoRisk-RS — Sala de Decisão SACE / Earth",
    page_icon="🌊",
    layout="wide",
)

MUNICIPIOS_RS_COORDS = {
    "Porto Alegre": [-30.0346, -51.2177], "Lajeado": [-29.4667, -51.9667],
    "Muçum": [-29.1667, -51.8667], "Encantado": [-29.2333, -51.8667],
    "Santa Tereza": [-29.1667, -51.7333], "São Leopoldo": [-29.7547, -51.1472],
    "Campo Bom": [-29.6833, -51.0500], "Montenegro": [-29.6833, -51.4667],
    "São Sebastião do Caí": [-29.5833, -51.3833], "Eldorado do Sul": [-30.0847, -51.3325],
    "Caxias do Sul": [-29.1681, -51.1794], "Santa Maria": [-29.6842, -53.8069],
    "Uruguaiana": [-29.7547, -57.0883], "Estrela": [-29.5000, -51.9667],
    "Bom Retiro do Sul": [-29.6019, -51.9464], "Taquari": [-29.7997, -51.8642],
}


# -----------------------------------------------------------------------------
# 2. COLETA / ATUALIZAÇÃO (a ligação com as fontes reais mora aqui)
# -----------------------------------------------------------------------------
def executar_coleta(incluir_ana: bool) -> None:
    """Dispara o coletor real e mostra o progresso."""
    barra = st.progress(0.0, text="Iniciando coleta…")

    def progresso(fracao: float, mensagem: str) -> None:
        barra.progress(min(max(fracao, 0.0), 1.0), text=mensagem)

    try:
        resumo = gd.sincronizar(incluir_ana=incluir_ana, progresso=progresso)
    except Exception as exc:
        barra.empty()
        st.error(f"Falha na coleta: {type(exc).__name__}: {exc}")
        return

    barra.empty()
    st.session_state["ultimo_resumo"] = resumo
    st.cache_data.clear()
    st.rerun()


@st.cache_data(ttl=30)
def obter_estacoes() -> pd.DataFrame:
    return gd.carregar_estacoes()


with st.sidebar:
    st.header("🔄 Atualização dos dados")

    ultima = gd.ultima_coleta()
    idade = gd.idade_dados()
    if ultima:
        minutos = int(idade.total_seconds() // 60) if idade else 0
        st.caption(
            f"Última coleta: **{ultima['terminada_em']}** "
            f"({minutos} min atrás)\n\n"
            f"Fontes: {ultima['fontes']} — {ultima['estacoes']} estações"
        )
    else:
        st.warning("Nenhuma coleta ainda. Clique em **Atualizar agora**.")

    auto = st.checkbox("Atualização automática", value=False)
    intervalo_min = st.number_input(
        "Reatualizar a cada (min):", min_value=5, max_value=180, value=15, step=5
    )
    incluir_ana = st.checkbox(
        "Incluir telemetria da ANA (mais lento, +500 estações)", value=False
    )

    if st.button("⬇️ Atualizar agora", use_container_width=True, type="primary"):
        executar_coleta(incluir_ana)

    st.divider()
    st.caption(
        "**Fontes reais:** SACE/SGB-CPRM (nível + cotas oficiais + série de "
        "15 min) e ANA/Telemetria (estações automáticas do RS).\n\n"
        "INMET não entra: hoje a API de leitura responde 204/404 sem token — "
        "preferimos nenhuma estação a uma estação com número inventado."
    )


@st.fragment(run_every="60s")
def vigia_atualizacao() -> None:
    """Roda sozinho a cada minuto; só dispara a coleta quando o dado envelhece.
    Fica num fragmento para não travar o resto da página."""
    if not auto:
        return
    idade_atual = gd.idade_dados()
    if idade_atual is None or idade_atual > timedelta(minutes=int(intervalo_min)):
        with st.spinner("Atualização automática em andamento…"):
            gd.sincronizar(incluir_ana=incluir_ana)
        st.cache_data.clear()
        st.rerun(scope="app")
    else:
        restante = int(intervalo_min) - int(idade_atual.total_seconds() // 60)
        st.caption(f"⏱️ Atualização automática ligada — próxima em ~{max(restante, 0)} min.")


# -----------------------------------------------------------------------------
# 3. CABEÇALHO
# -----------------------------------------------------------------------------
col_t1, col_t2 = st.columns([3, 1])
with col_t1:
    st.title("🌊 GeoRisk-RS — Sistema Unificado de Monitoramento & Alerta")
    st.caption(
        "Dados reais do SACE/SGB-CPRM e da telemetria da ANA, gravados no banco "
        "`georisk_rs.db` e padronizados em um formato único."
    )
with col_t2:
    vigia_atualizacao()

df = obter_estacoes()

if df.empty:
    st.warning(
        "O banco está acessível, mas ainda não há estação coletada. "
        "Use **Atualizar agora** na barra lateral para buscar os dados reais."
    )
    st.stop()

# -----------------------------------------------------------------------------
# 4. FILTROS E INDICADORES
# -----------------------------------------------------------------------------
with st.expander("🎛️ Filtros", expanded=False):
    c1, c2, c3 = st.columns(3)
    with c1:
        fontes = st.multiselect(
            "Fonte:", sorted(df["fonte"].dropna().unique()),
            default=sorted(df["fonte"].dropna().unique()),
        )
    with c2:
        bacias = st.multiselect(
            "Bacia:", sorted(df["bacia"].dropna().unique()),
            default=sorted(df["bacia"].dropna().unique()),
        )
    with c3:
        so_com_dado = st.checkbox("Somente estações transmitindo agora", value=False)

df_f = df[df["fonte"].isin(fontes) & df["bacia"].isin(bacias)].copy()
if so_com_dado:
    df_f = df_f[df_f["nivel_cm"].notna() | df_f["chuva_24h"].notna()]

df_fluvio = df_f[df_f["tipo"] == "FLUVIOMETRICA"].copy()

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Estações no painel", len(df_f))
m2.metric("Inundação 🔴", int((df_f["cor"] == "red").sum()))
m3.metric("Alerta 🟧", int((df_f["cor"] == "orange").sum()))
m4.metric("Atenção 🟨", int((df_f["cor"] == "gold").sum()))
maior_chuva = df_f["chuva_24h"].max()
m5.metric(
    "Maior chuva 24h",
    f"{maior_chuva:.1f} mm" if pd.notna(maior_chuva) else "Sem dado",
)

st.divider()


# -----------------------------------------------------------------------------
# 5. MOTOR DE MORFOLOGIA DAS MANCHAS (visual esquemático — ver aviso na aba 2)
# -----------------------------------------------------------------------------
def gerar_morfologia_rio_e_mancha(lat_centro, lon_centro, fator_largura=1.0):
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
# 6. BOLETIM — gráfico com a SÉRIE REAL gravada no banco
# -----------------------------------------------------------------------------
@st.cache_data(ttl=120)
def obter_serie(id_estacao: str, grandeza: str) -> pd.DataFrame:
    return gd.carregar_serie(id_estacao, grandeza)


def gerar_grafico_boletim(linha: pd.Series) -> go.Figure | None:
    serie_cota = obter_serie(linha["id"], "cota")
    serie_chuva = obter_serie(linha["id"], "chuva")

    if serie_cota.empty and serie_chuva.empty:
        return None

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    if not serie_cota.empty:
        fig.add_trace(
            go.Scatter(
                x=serie_cota["datahora"], y=serie_cota["valor"], mode="lines",
                name="Cota (cm)", line=dict(color="#0066cc", width=3),
            ),
            secondary_y=False,
        )

    if not serie_chuva.empty:
        # Soma horária para a barra não virar um borrão de 15 em 15 min.
        chuva_h = (
            serie_chuva.set_index("datahora")["valor"].resample("h").sum().reset_index()
        )
        fig.add_trace(
            go.Bar(
                x=chuva_h["datahora"], y=chuva_h["valor"], name="Chuva (mm/h)",
                marker_color="#777777", opacity=0.65,
            ),
            secondary_y=True,
        )

    for chave, cor, rotulo in [
        ("cota_atencao_cm", "#e6c200", "Atenção"),
        ("cota_alerta_cm", "#ff7f0e", "Alerta"),
        ("cota_inundacao_cm", "#d62728", "Inundação"),
    ]:
        valor = linha.get(chave)
        if pd.notna(valor):
            fig.add_hline(
                y=float(valor), line_color=cor, line_width=2, line_dash="dash",
                annotation_text=f"{rotulo}: {int(valor)} cm", secondary_y=False,
            )

    fig.update_layout(
        title=f"<b>{linha['nome']}</b> — série real da fonte ({linha['fonte']})",
        height=430, hovermode="x unified", bargap=0.05,
        plot_bgcolor="#ffffff", paper_bgcolor="#ffffff", font=dict(color="#000000"),
        legend=dict(orientation="h", y=1.12, x=0.0),
        margin=dict(l=40, r=40, t=80, b=40),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e8e8e8")
    fig.update_yaxes(title_text="<b>Cota (cm)</b>", secondary_y=False,
                     showgrid=True, gridcolor="#e8e8e8")
    fig.update_yaxes(title_text="<b>Chuva (mm/h)</b>", secondary_y=True, showgrid=False)
    return fig


def _mostrar(valor, sufixo: str = "", casas: int = 0) -> str:
    if valor is None or pd.isna(valor):
        return "Sem dado"
    return f"{float(valor):.{casas}f}{sufixo}"


def _ou(valor, padrao: str = "—") -> str:
    """Texto do banco com fallback. Campo vazio chega do SQLite como NaN, que
    é 'verdadeiro' em Python — por isso `valor or padrao` não serve aqui."""
    if valor is None or pd.isna(valor) or str(valor).strip() == "":
        return padrao
    return str(valor)


@st.dialog("📋 Boletim da estação — dados reais", width="large")
def abrir_dialog_boletim(linha: pd.Series) -> None:
    st.markdown(f"### {linha['nome']}")
    st.markdown(
        f"**Fonte:** {linha['fonte']} &nbsp;|&nbsp; **Código:** {_ou(linha.get('codigo'))} "
        f"&nbsp;|&nbsp; **Bacia:** {_ou(linha.get('bacia'))} "
        f"&nbsp;|&nbsp; **Rio:** {_ou(linha.get('rio'), 'Não informado')}"
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Nível atual", _mostrar(linha.get("nivel_cm"), " cm"))
    c2.metric("Situação", _ou(linha.get("situacao")))
    c3.metric("Chuva 24h", _mostrar(linha.get("chuva_24h"), " mm", 1))
    c4.metric("Vazão", _mostrar(linha.get("vazao_m3s"), " m³/s", 1))
    st.caption(f"Medição da fonte em: {_ou(linha.get('medido_em'), 'sem registro')}")

    # pd.notna: vindo do banco, campo vazio chega como NaN (que é "verdadeiro").
    if pd.notna(linha.get("observacao")):
        st.warning(
            f"Campo descartado por estar fora da faixa física: {linha['observacao']}. "
            "Preferimos deixar vazio a exibir um valor impossível."
        )

    figura = gerar_grafico_boletim(linha)
    if figura is None:
        st.info(
            "Esta estação ainda não tem série histórica no banco. Séries de "
            "15 min só existem para as estações do SACE — rode a coleta com o "
            "modo completo se necessário."
        )
    else:
        st.plotly_chart(figura, use_container_width=True)

    # ---- Nowcast: extrapolação da chuva REAL recente, não previsão de tempo
    st.markdown("#### 🌧️ Nowcast de chuva")
    st.caption(
        "Extrapolação estatística da taxa de chuva realmente medida nas últimas "
        "horas. **Não é previsão meteorológica** — não usa modelo atmosférico."
    )
    nowcast = gd.nowcast_chuva(linha["id"])
    if nowcast is None:
        st.info("Sem leituras de chuva recentes suficientes para extrapolar.")
    else:
        n1, n2, n3 = st.columns(3)
        n1.metric("Taxa recente", f"{nowcast['taxa_recente_mm_h']:.1f} mm/h")
        n2.metric("Projeção +12h", f"{nowcast['acumulado_previsto_12h']:.1f} mm")
        n3.metric("Projeção +24h", f"{nowcast['acumulado_previsto_24h']:.1f} mm")
        st.caption(
            f"Base: {nowcast['base_horas']} h até {nowcast['ate']:%d/%m %H:%M}. "
            f"Cobertura de amostras na janela: {nowcast['confiabilidade'] * 100:.0f}%."
        )

    # ---- Download do dado real
    serie_cota = obter_serie(linha["id"], "cota")
    serie_chuva = obter_serie(linha["id"], "chuva")
    d1, d2 = st.columns(2)
    if not serie_cota.empty:
        d1.download_button(
            "📥 Série de cota (CSV)", serie_cota.to_csv(index=False).encode("utf-8"),
            file_name=f"cota_{linha['id']}.csv", mime="text/csv", use_container_width=True,
        )
    if not serie_chuva.empty:
        d2.download_button(
            "📥 Série de chuva (CSV)", serie_chuva.to_csv(index=False).encode("utf-8"),
            file_name=f"chuva_{linha['id']}.csv", mime="text/csv", use_container_width=True,
        )

    if pd.notna(linha.get("url_origem")):
        st.caption(f"Origem do dado: {linha['url_origem']}")


def estacao_clicada(retorno_mapa, candidatas: pd.DataFrame, tolerancia: float = 0.02):
    """Casa o clique do mapa com a estação mais próxima."""
    if not retorno_mapa or not retorno_mapa.get("last_object_clicked"):
        return None
    clique = retorno_mapa["last_object_clicked"]
    validas = candidatas.dropna(subset=["lat", "lon"]).copy()
    if validas.empty:
        return None
    validas["_dist"] = np.hypot(
        validas["lat"] - clique["lat"], validas["lon"] - clique["lng"]
    )
    melhor = validas.nsmallest(1, "_dist").iloc[0]
    return melhor if melhor["_dist"] < tolerancia else None


# -----------------------------------------------------------------------------
# 7. ABAS
# -----------------------------------------------------------------------------
tab1, tab2 = st.tabs(
    ["📍 Estações & Monitoramento", "🗺️ Manchas de Inundação (esquemático)"]
)

with tab1:
    c_busca, c_legenda = st.columns([1, 2])
    with c_busca:
        cid_tab1 = st.selectbox(
            "🔍 Localizar Cidade do RS:",
            ["-- Selecionar Cidade --"] + sorted(MUNICIPIOS_RS_COORDS),
            key="sb_t1",
        )
    with c_legenda:
        st.markdown(
            "**Legenda:** 🟢 Normal &nbsp; 🟡 Atenção &nbsp; 🟠 Alerta &nbsp; "
            "🔴 Inundação &nbsp; ⚪ Sem cota oficial publicada / sem transmissão"
        )

    centro = MUNICIPIOS_RS_COORDS.get(cid_tab1, [-29.7, -51.8])
    zoom = 12 if cid_tab1 != "-- Selecionar Cidade --" else 7

    mapa = folium.Map(location=centro, zoom_start=zoom, tiles="CartoDB positron")
    for _, r in df_f.dropna(subset=["lat", "lon"]).iterrows():
        nivel_txt = _mostrar(r.get("nivel_cm"), " cm")
        chuva_txt = _mostrar(r.get("chuva_24h"), " mm", 1)
        folium.CircleMarker(
            location=[r["lat"], r["lon"]],
            radius=8 if r["tipo"] == "FLUVIOMETRICA" else 5,
            color=r["cor"], fill=True, fill_color=r["cor"], fill_opacity=0.85, weight=1,
            tooltip=(
                f"<b>{r['nome']}</b><br>{r['situacao']}<br>"
                f"Nível: {nivel_txt}<br>Chuva 24h: {chuva_txt}<br>"
                f"<i>{r['fonte']}</i>"
            ),
        ).add_to(mapa)

    retorno = st_folium(mapa, width="100%", height=520, key="mapa_t1")

    selecionada = estacao_clicada(retorno, df_f)
    if selecionada is not None:
        abrir_dialog_boletim(selecionada)

    st.markdown("#### 📊 Estações fluviométricas — cota real x cota oficial")
    colunas_tabela = [
        "nome", "rio", "bacia", "nivel_cm", "cota_atencao_cm",
        "cota_alerta_cm", "cota_inundacao_cm", "situacao", "medido_em", "fonte",
    ]
    st.dataframe(
        df_fluvio[colunas_tabela].sort_values("nome"),
        column_config={
            "nome": "Estação", "rio": "Rio", "bacia": "Bacia",
            "nivel_cm": st.column_config.NumberColumn("Nível (cm)", format="%d"),
            "cota_atencao_cm": st.column_config.NumberColumn("Atenção", format="%d"),
            "cota_alerta_cm": st.column_config.NumberColumn("Alerta", format="%d"),
            "cota_inundacao_cm": st.column_config.NumberColumn("Inundação", format="%d"),
            "situacao": "Situação", "medido_em": "Medido em", "fonte": "Fonte",
        },
        hide_index=True, use_container_width=True, height=300,
    )

with tab2:
    st.markdown("### 🗺️ Manchas de Inundação — visualização esquemática")
    st.warning(
        "⚠️ **Estas manchas NÃO são mancha de inundação medida.** O traçado é "
        "gerado matematicamente ao redor da estação, só para dar noção de "
        "extensão. O que é real aqui é a **cor**, que vem da cota medida "
        "comparada à cota oficial. Para mancha oficial, some uma camada WMS da "
        "Defesa Civil/SGB.",
        icon="⚠️",
    )

    col_ctrl, col_mapa = st.columns([1, 3])
    with col_ctrl:
        cid_tab2 = st.selectbox(
            "Ir para Cidade do RS:",
            ["-- Selecionar Cidade --"] + sorted(MUNICIPIOS_RS_COORDS),
            key="sb_t2",
        )
        st.markdown("---")
        v_atencao = st.checkbox("🟡 Camada - Cota de Atenção", value=True)
        v_alerta = st.checkbox("🟠 Camada - Cota de Alerta", value=True)
        v_inundacao = st.checkbox("🔴 Camada - Cota de Inundação", value=True)
        v_eixo = st.checkbox("🟢 Exibir Eixo do Rio (Traçado)", value=True)
        st.markdown("---")
        opac = st.slider("Opacidade da Mancha (%):", 20, 100, 60) / 100.0
        so_criticas = st.checkbox(
            "Somente estações em atenção/alerta/inundação", value=True,
            help="Desenhar as ~500 estações deixa o mapa pesado e ilegível.",
        )

    with col_mapa:
        centro2 = MUNICIPIOS_RS_COORDS.get(cid_tab2, [-29.6833, -51.4667])
        zoom2 = 12 if cid_tab2 != "-- Selecionar Cidade --" else 8

        mapa_sat = folium.Map(
            location=centro2, zoom_start=zoom2,
            tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attr="Esri World Imagery",
        )
        folium.TileLayer(
            tiles="https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png",
            attr="CartoDB Labels", overlay=True,
        ).add_to(mapa_sat)

        alvos = df_fluvio.dropna(subset=["lat", "lon"])
        if so_criticas:
            alvos = alvos[alvos["cor"].isin(["gold", "orange", "red", "purple"])]

        for _, est in alvos.iterrows():
            rio = _ou(est.get("rio"), est["nome"])
            poly_at, eixo = gerar_morfologia_rio_e_mancha(est["lat"], est["lon"], 1.8)
            poly_al, _ = gerar_morfologia_rio_e_mancha(est["lat"], est["lon"], 1.3)
            poly_in, _ = gerar_morfologia_rio_e_mancha(est["lat"], est["lon"], 0.8)

            if v_atencao:
                folium.Polygon(
                    locations=poly_at, color="#fbc02d", fill_color="#ffeb3b",
                    fill_opacity=opac, weight=1,
                    tooltip=f"Várzea Atenção: {rio}",
                ).add_to(mapa_sat)
            if v_alerta:
                folium.Polygon(
                    locations=poly_al, color="#e65100", fill_color="#ff9800",
                    fill_opacity=opac, weight=1,
                    tooltip=f"Área Alerta: {rio}",
                ).add_to(mapa_sat)
            if v_inundacao:
                folium.Polygon(
                    locations=poly_in, color="#b71c1c", fill_color="#f44336",
                    fill_opacity=opac, weight=1.5,
                    tooltip=f"Inundação Severa: {rio}",
                ).add_to(mapa_sat)
            if v_eixo:
                folium.PolyLine(
                    locations=eixo, color="#00e676", weight=2.5, dash_array="6, 6",
                    opacity=0.9, tooltip=f"Eixo do Rio: {rio}",
                ).add_to(mapa_sat)

        st_folium(mapa_sat, width="100%", height=600, key="mapa_satelite_morfologico")
        st.caption(f"{len(alvos)} estação(ões) desenhada(s) com cor de risco real.")
