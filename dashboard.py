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
        f"🔄 Atualiza sozinho a cada **{INTERVALO_ATUALIZACAO_MIN} min**, "
        "com SACE + telemetria da ANA."
    )

    if st.button("⬇️ Atualizar agora", use_container_width=True, type="primary"):
        atualizar(INCLUIR_ANA)

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
def avisar_defasagem(df: pd.DataFrame, limite_horas: float = 3.0) -> None:
    """Avisa quando uma fonte ficou para trás das outras.

    Existe porque é fácil coletar só o SACE (mais rápido) e esquecer que as
    ~500 estações da ANA continuam no mapa com a leitura da coleta anterior.
    O painel mostrava o horário da ÚLTIMA coleta e dava a impressão de que
    tudo ali era daquele momento — enquanto metade do mapa podia estar com
    dado de dois dias antes.
    """
    if "atualizado_em" not in df or df["atualizado_em"].isna().all():
        return

    quando = pd.to_datetime(df["atualizado_em"], errors="coerce")
    por_fonte = (
        df.assign(_quando=quando)
        .dropna(subset=["_quando"])
        .groupby("fonte")["_quando"]
        .agg(["max", "count"])
        .sort_values("max")
    )
    if len(por_fonte) < 2:
        return

    mais_nova = por_fonte["max"].max()
    atrasadas = por_fonte[
        (mais_nova - por_fonte["max"]) > pd.Timedelta(hours=limite_horas)
    ]
    if atrasadas.empty:
        return

    detalhe = " · ".join(
        f"**{fonte}**: {int(linha['count'])} estações paradas em "
        f"{linha['max']:%d/%m %H:%M} "
        f"({(mais_nova - linha['max']).total_seconds() / 3600:.0f} h atrás)"
        for fonte, linha in atrasadas.iterrows()
    )
    st.warning(
        f"⏳ **Fontes fora de sincronia.** {detalhe}. Elas continuam no mapa "
        "com a leitura antiga. Para igualar, marque *Incluir telemetria da ANA* "
        "na barra lateral e clique em **Atualizar agora**.",
        icon="⏳",
    )


avisar_defasagem(df_estacoes)

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
# MANCHAS OFICIAIS — georisk_geo (SGB/IPH-UFRGS + Defesa Civil RS)
# -----------------------------------------------------------------------------
@st.cache_data(ttl=600)
def _inventario_manchas() -> list[dict]:
    return gg.municipios_com_mancha()


@st.cache_data(ttl=600)
def _mancha_por_cota(municipio: str, cota_cm: float | None) -> dict | None:
    return gg.mancha_para_cota(municipio, cota_cm)


@st.cache_data(ttl=600)
def _mancha_evento(municipio: str) -> dict | None:
    return gg.mancha_de_evento(municipio)


def _candidatos_de_nome(linha) -> list[str]:
    """Nomes possíveis do município de uma estação.

    A estação do SACE tem só o nome do posto (ex.: "Passo Montenegro"), e a
    mancha é catalogada pelo município ("Montenegro"). Tentamos os dois campos
    e também cada palavra, porque o casamento é feito com normalização.
    """
    nomes = []
    for campo in ("municipio", "nome"):
        valor = linha.get(campo)
        if valor is not None and not pd.isna(valor):
            texto = str(valor).strip()
            if texto:
                nomes.append(texto)
                nomes.extend(p for p in texto.split() if len(p) > 3)
    return nomes


def desenhar_manchas_oficiais(mapa, estacoes, opacidade: float,
                              mostrar_cota: bool, mostrar_evento: bool,
                              camadas=("atencao", "alerta", "inundacao", "atual"),
                              mostrar_faixas: bool = True) -> dict:
    """Desenha as manchas OFICIAIS de cada estação, uma por nível de risco.

    Em vez de uma mancha só, projeta as TRÊS COTAS DE RISCO da estação —
    atenção (amarelo), alerta (laranja) e inundação (vermelho) — mais,
    opcionalmente, a mancha do nível medido agora (azul-escuro).

    Por que três e não uma: mostrar apenas a cota atual é enganoso em rio
    baixo. Com Montenegro em 300 cm a única mancha compatível é a da calha, e
    o mapa dá a impressão de que "a mancha é só o rio". As três cotas
    respondem a pergunta que interessa ao operador — até onde a água chega SE
    subir até cada limiar oficial.

    As manchas são aninhadas (a de inundação contém a de alerta, que contém a
    de atenção), então desenhamos da maior para a menor: assim todas ficam
    visíveis, em vez de a maior cobrir as outras.
    """
    resumo = {"por_cota": [], "por_evento": [], "por_faixa": [], "sem_mancha": []}
    ja_desenhado = set()

    # Ordem de desenho: maior área primeiro (fica por baixo).
    NIVEIS = [
        ("inundacao", "cota_inundacao_cm", "Inundação", "#f44336", "#b71c1c"),
        ("alerta", "cota_alerta_cm", "Alerta", "#ff9800", "#e65100"),
        ("atencao", "cota_atencao_cm", "Atenção", "#ffeb3b", "#fbc02d"),
    ]

    for _, est in estacoes.iterrows():
        nomes = _candidatos_de_nome(est)
        nivel = est.get("nivel_cm")
        nivel = None if nivel is None or pd.isna(nivel) else float(nivel)
        achou = False

        if mostrar_cota:
            # --- As três cotas oficiais de risco
            for chave_camada, campo, rotulo, preenche, borda in NIVEIS:
                if chave_camada not in camadas:
                    continue
                limiar = est.get(campo)
                if limiar is None or pd.isna(limiar):
                    continue
                limiar = float(limiar)

                for nome in nomes:
                    mancha = _mancha_por_cota(nome, limiar)
                    if not mancha:
                        continue
                    chave = ("cota", mancha["municipio"], mancha["cota_cm"])
                    if chave in ja_desenhado:
                        achou = True
                        break
                    folium.GeoJson(
                        mancha["geojson"],
                        name=f"{rotulo} — {mancha['municipio']}",
                        style_function=(
                            lambda _f, o=opacidade, p=preenche, b=borda: {
                                "fillColor": p, "color": b,
                                "weight": 1.5, "fillOpacity": o,
                            }
                        ),
                        tooltip=(
                            f"<b>Cota de {rotulo} — {mancha['municipio']}</b><br>"
                            f"Limiar oficial: {limiar:.0f} cm<br>"
                            f"Mancha mapeada: {mancha['rotulo']}<br>"
                            f"Nível agora: {nivel:.0f} cm<br>" if nivel is not None
                            else f"<b>Cota de {rotulo} — {mancha['municipio']}</b><br>"
                                 f"Limiar oficial: {limiar:.0f} cm<br>"
                                 f"Mancha mapeada: {mancha['rotulo']}<br>"
                        ) + f"<i>{mancha['fonte']}</i>",
                    ).add_to(mapa)
                    ja_desenhado.add(chave)
                    resumo["por_cota"].append(
                        f"{mancha['municipio']} {rotulo} ({int(mancha['cota_cm'])} cm)"
                    )
                    achou = True
                    break

            # --- Mancha do nível medido agora, por cima de todas
            if "atual" in camadas and nivel is not None:
                for nome in nomes:
                    mancha = _mancha_por_cota(nome, nivel)
                    if not mancha:
                        continue
                    chave = ("atual", mancha["municipio"], mancha["cota_cm"])
                    if chave in ja_desenhado:
                        achou = True
                        break
                    folium.GeoJson(
                        mancha["geojson"],
                        name=f"Nível atual — {mancha['municipio']}",
                        style_function=lambda _f: {
                            "fillColor": "#0d47a1", "color": "#01579b",
                            "weight": 2.5, "fillOpacity": 0.0, "dashArray": "6,4",
                        },
                        tooltip=(
                            f"<b>NÍVEL AGORA — {mancha['municipio']}</b><br>"
                            f"Medido: {nivel:.0f} cm<br>"
                            f"Mancha: {mancha['rotulo']}<br>"
                            f"<i>{mancha['fonte']}</i>"
                        ),
                    ).add_to(mapa)
                    ja_desenhado.add(chave)
                    achou = True
                    break

        if mostrar_evento:
            for nome in nomes:
                evento = _mancha_evento(nome)
                if not evento:
                    continue
                chave = ("evento", evento["municipio"])
                if chave not in ja_desenhado:
                    folium.GeoJson(
                        evento["geojson"],
                        name=f"Evento {evento['municipio']}",
                        style_function=lambda _f, o=opacidade: {
                            "fillColor": "#7b1fa2", "color": "#4a148c",
                            "weight": 1.2, "fillOpacity": o * 0.65,
                        },
                        tooltip=(
                            f"<b>{evento['municipio']} — {evento['rotulo']}</b><br>"
                            f"<i>{evento['fonte']}</i>"
                        ),
                    ).add_to(mapa)
                    ja_desenhado.add(chave)
                    resumo["por_evento"].append(evento["municipio"])
                achou = True
                break

        # --- FAIXAS SOBRE A HIDROGRAFIA OFICIAL
        # Só 5 municípios têm mancha modelada pelo SGB. Para os demais — que
        # incluem Estrela, Encantado e Muçum — desenhamos faixas ao longo do
        # curso REAL do rio (hidrografia do IBGE, 1:100.000), com largura
        # estimada pela cota. O traçado é oficial; a largura é estimativa,
        # não mancha modelada.
        if not achou and mostrar_faixas:
            cotas = {
                "atencao": est.get("cota_atencao_cm"),
                "alerta": est.get("cota_alerta_cm"),
                "inundacao": est.get("cota_inundacao_cm"),
            }
            if any(pd.notna(v) for v in cotas.values()):
                try:
                    faixas = gg.faixas_de_risco(
                        est["lat"], est["lon"], cotas,
                        nome_rio=(None if pd.isna(est.get("rio")) else est.get("rio")),
                    )
                except Exception:
                    faixas = []
                for faixa in faixas:
                    if faixa["nivel"] not in camadas:
                        continue
                    folium.GeoJson(
                        faixa["geojson"],
                        name=f"{faixa['rotulo']} — {est['nome']}",
                        style_function=(
                            lambda _f, o=opacidade, p=faixa["cor_preenchimento"],
                                   b=faixa["cor_borda"]: {
                                "fillColor": p, "color": b,
                                "weight": 1, "fillOpacity": o * 0.75,
                                "dashArray": "4,3",
                            }
                        ),
                        tooltip=(
                            f"<b>{est['nome']} — Cota de {faixa['rotulo']}</b><br>"
                            f"Limiar oficial: {faixa['cota_cm']:.0f} cm<br>"
                            f"Faixa estimada: ±{faixa['largura_estimada_m']:.0f} m<br>"
                            f"<i>Traçado do rio: IBGE 1:100.000. Largura é "
                            f"ESTIMATIVA, não mancha modelada.</i>"
                        ),
                    ).add_to(mapa)
                if faixas:
                    achou = True
                    resumo.setdefault("por_faixa", []).append(est["nome"])

        if not achou:
            resumo["sem_mancha"].append(est.get("nome"))

    return resumo

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
        ver_faixas = st.checkbox(
            "🌊 Faixas sobre o rio real (onde não há mancha oficial)", value=True,
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
        resumo_oficial = desenhar_manchas_oficiais(
            mapa_sat, alvos, opac_val, ver_oficial_cota, ver_oficial_evento,
            camadas=_camadas, mostrar_faixas=ver_faixas,
        )

        for _, est in (alvos.iterrows() if ver_esquematico else iter([])):
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
        "Correlação cruzada chuva x taxa de subida do rio para obter o tempo de "
        "resposta (Tc), balanço volumétrico SCS-CN com umidade antecedente de "
        "72 h, e projeção da cota até cruzar as cotas oficiais do SACE."
    )

    @st.cache_data(ttl=300)
    def _analisaveis() -> pd.DataFrame:
        return gh.estacoes_analisaveis()

    @st.cache_data(ttl=300, show_spinner="Modelando resposta da bacia…")
    def _analisar(estacao_id: str, cn: float) -> dict:
        """Cacheia só os DADOS, nunca a figura.

        Guardar o objeto Figure do Plotly no cache fazia o Streamlit reusar o
        mesmo nó entre reruns e o React quebrava com
        `NotFoundError: Failed to execute 'removeChild' on 'Node'`. A figura é
        barata de remontar; a modelagem é que é cara.
        """
        resultado = gh.estimar_tempo_e_impacto_inundacao(estacao_id, cn_base=cn)
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

    elegiveis = _analisaveis()

    if elegiveis.empty:
        st.warning(
            "Nenhuma estação tem as duas séries (cota e chuva) no banco. Rode uma "
            "coleta incluindo o SACE — é ele que publica a série de 15 em 15 min."
        )
    else:
        c_sel, c_cn = st.columns([3, 1])
        with c_sel:
            rotulos = {
                f"{r.nome} — {r.rio or 'rio não informado'} "
                f"({r.chuva_mm_periodo:.0f} mm no período)": r.id
                for r in elegiveis.itertuples()
            }
            escolhido = st.selectbox("Estação:", list(rotulos), key="hidro_estacao")
            estacao_id = rotulos[escolhido]
        with c_cn:
            cn = st.slider(
                "Curve Number (CN):", 40, 95, int(gh.CN_PADRAO),
                help="Parâmetro de escoamento, não medição. 75 = bacia rural mista "
                     "em solo B/C. Maior CN = solo que infiltra menos.",
            )

        resultado = _analisar(estacao_id, float(cn))

        # --- Indicadores
        k1, k2, k3, k4 = st.columns(4)
        aferic = resultado.get("afericao_tc")
        k1.metric(
            "Tempo de resposta (Tc)",
            f"{resultado['tc_horas']:.1f} h" if resultado["tc_horas"] else "Indeterminado",
            help="Defasagem de maior correlação entre a chuva e a subida do rio."
                 + (f" Aferição pela densidade de drenagem ({aferic['densidade_drenagem']}): "
                    f"esperado {aferic['tc_esperado_h'][0]:.0f}–{aferic['tc_esperado_h'][1]:.0f} h — "
                    f"{aferic['veredito']}." if aferic else ""),
        )
        k2.metric("Cota atual", texto(resultado["cota_atual_cm"], " cm"))
        k3.metric(
            "Cota máxima projetada",
            texto(resultado["cota_maxima_projetada_cm"], " cm"),
            delta=(
                None
                if resultado["cota_maxima_projetada_cm"] is None
                or resultado["cota_atual_cm"] is None
                else f"{resultado['cota_maxima_projetada_cm'] - resultado['cota_atual_cm']:+.0f} cm"
            ),
        )
        horas = resultado["tempo_horas_ate_inundacao"]
        k4.metric(
            "Até a Cota de Inundação",
            "JÁ ULTRAPASSADA" if horas == 0
            else (f"{horas:.1f} h" if horas is not None else "Não projetada"),
        )

        # --- Confiabilidade em destaque: é o que decide se dá para usar
        if resultado["confiavel"]:
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
        st.plotly_chart(figura, use_container_width=True, key=f"hidro_{estacao_id}")

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
                hide_index=True, use_container_width=True,
            )
            st.markdown("##### Precipitação acumulada")
            st.dataframe(
                pd.DataFrame(
                    list(resultado["precipitacao_acumulada_mm"].items()),
                    columns=["Janela", "mm"],
                ),
                hide_index=True, use_container_width=True,
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
                    hide_index=True, use_container_width=True,
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
                hide_index=True, use_container_width=True,
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
