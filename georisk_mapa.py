"""
GeoRisk-RS — DESENHO DO MAPA
============================
Funções de mapa compartilhadas por `main.py` e `dashboard.py`.

POR QUE ESTE MÓDULO EXISTE
--------------------------
As duas telas nasceram com cópias das mesmas funções, e elas divergiram: quando
o modo de envelope da projeção entrou no `dashboard.py`, a `main.py` ficou para
trás — 58 linhas de diferença só em `desenhar_manchas_oficiais`, e as quatro
cores de risco existiam em apenas um dos painéis.

Duplicação de código de desenho é assim: a correção vai para um lado e o outro
segue com o defeito, sem ninguém perceber, porque os dois continuam abrindo sem
erro. Aqui há uma implementação só.

O QUE FICA AQUI
---------------
  - casamento de estação com mancha oficial (SGB por cota, Defesa Civil por evento)
  - desenho das faixas de risco sobre a hidrografia oficial
  - aviso de fontes fora de sincronia
  - traçado esquemático (senos e cossenos), mantido como opção desligada

O QUE NÃO FICA
--------------
Layout, controles e textos de cada painel — esses são de cada tela e devem
mesmo ser diferentes.
"""

from __future__ import annotations

import folium
import numpy as np
import pandas as pd
import streamlit as st

import georisk_geo as gg
import georisk_hidrologia as gh


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


@st.cache_data(ttl=600, show_spinner=False)
def _projecao_cacheada(estacao_id: str) -> dict:
    """Projeção da estação, sem a figura (que não é serializável no cache)."""
    resultado = gh.estimar_tempo_e_impacto_inundacao(estacao_id)
    resultado.pop("grafico_hietograma_hidrograma", None)
    return resultado


def desenhar_manchas_oficiais(mapa, estacoes, opacidade: float,
                              mostrar_cota: bool, mostrar_evento: bool,
                              camadas=("atencao", "alerta", "inundacao", "atual"),
                              mostrar_faixas: bool = True,
                              modo_faixa: str = "envelope") -> dict:
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
            rio_nome = None if pd.isna(est.get("rio")) else est.get("rio")
            faixas = []
            try:
                if modo_faixa == "envelope":
                    # Mancha da COTA PROJETADA, cores = incerteza do modelo.
                    # Cacheado: rodar o modelo por estação a cada rerun do
                    # mapa levava ~30 s para 10 estações.
                    proj = _projecao_cacheada(est["id"])
                    pontos = proj.get("projecao") or []
                    if pontos:
                        # Horizonte útil se existir; senão o primeiro.
                        util = proj.get("horizonte_util_horas")
                        alvo_p = next(
                            (p for p in pontos if util and p["horas_a_frente"] <= util),
                            pontos[0],
                        )
                        faixas = gg.faixas_de_incerteza(
                            est["lat"], est["lon"],
                            alvo_p["cota_minima_cm"], alvo_p["cota_projetada_cm"],
                            alvo_p["cota_maxima_cm"], nome_rio=rio_nome,
                        )
                elif any(pd.notna(v) for v in cotas.values()):
                    faixas = gg.faixas_de_risco(
                        est["lat"], est["lon"], cotas, nome_rio=rio_nome,
                    )
            except Exception:
                faixas = []

                for faixa in faixas:
                    if modo_faixa == "limiares" and faixa["nivel"] not in camadas:
                        continue
                    folium.GeoJson(
                        faixa["geojson"],
                        name=f"{faixa['rotulo']} — {est['nome']}",
                        style_function=(
                            # A área segura é desenhada quase transparente: ela
                            # é a MAIOR de todas e, cheia, esconderia as faixas
                            # de risco que ficam por dentro. O que interessa
                            # nela é o contorno, que marca o limite do alcance.
                            lambda _f, o=opacidade, p=faixa["cor_preenchimento"],
                                   b=faixa["cor_borda"],
                                   seg=faixa.get("eh_area_segura", False): {
                                "fillColor": p, "color": b,
                                "weight": 2 if seg else 1,
                                "fillOpacity": 0.10 if seg else o * 0.75,
                                "dashArray": "6,4" if seg else "4,3",
                            }
                        ),
                        tooltip=(
                            f"<b>{est['nome']} — {faixa['rotulo']}</b><br>"
                            f"Cota: {faixa['cota_cm']:.0f} cm<br>"
                            + (f"<i>{faixa['leitura']}</i><br>"
                               if faixa.get("leitura") else "")
                            + f"Faixa estimada: ±{faixa['largura_estimada_m']:.0f} m<br>"
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


