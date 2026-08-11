"""
GeoRisk-RS — RELEVO (Modelo Digital de Elevação)
================================================
Destrava três coisas que ficaram bloqueadas por falta de topografia:

  1. **Área contribuinte real** de cada estação, delimitada por escoamento
     sobre o terreno em vez de inferida por correlação entre pluviômetros.
  2. **Kirpich aplicável**, porque agora há comprimento de talvegue e
     declividade — os dois parâmetros que faltavam.
  3. **Mancha de inundação por cota**, projetando o nível sobre o terreno em
     vez de estimar uma largura proporcional.

FONTE
-----
Copernicus DEM GLO-30, resolução de 30 m, aberto e sem autenticação:

    https://copernicus-dem-30m.s3.amazonaws.com/

São GeoTIFF em formato COG (Cloud Optimized), o que permite ler **apenas a
janela de interesse** pela rede, com `/vsicurl/`, sem baixar o tile inteiro de
49 MB. Medido: abrir o tile leva ~5 s e recortar a vizinhança de uma estação,
~3 s.

Cada tile cobre 1° × 1°. O RS inteiro seriam ~63 tiles e uns 3 GB — por isso
nada é baixado por atacado: recorta-se por estação e guarda-se o recorte no
banco.

DEPENDÊNCIA
-----------
`rasterio` (wheels modernas já trazem o GDAL embutido, sem instalação
separada). É a única dependência pesada do projeto e só este módulo a usa —
os painéis funcionam sem ela, apenas sem os recursos daqui.
"""

from __future__ import annotations

import math

import numpy as np

BASE_COPERNICUS = "https://copernicus-dem-30m.s3.amazonaws.com"

# Metros por grau de latitude (a de longitude depende do cosseno da latitude).
METROS_POR_GRAU_LAT = 110540.0


def _nome_tile(lat: float, lon: float) -> str:
    """Nome do tile Copernicus que contém o ponto.

    Os tiles são nomeados pelo canto INFERIOR ESQUERDO em graus inteiros:
    `Copernicus_DSM_COG_10_S30_00_W052_00_DEM` cobre de 30°S a 29°S e de 52°W
    a 51°W. Daí o `floor` — e não arredondamento.
    """
    lat_i, lon_i = math.floor(lat), math.floor(lon)
    ns = "N" if lat_i >= 0 else "S"
    ew = "E" if lon_i >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_10_{ns}{abs(lat_i):02d}_00_"
        f"{ew}{abs(lon_i):03d}_00_DEM"
    )


def url_tile(lat: float, lon: float) -> str:
    nome = _nome_tile(lat, lon)
    return f"/vsicurl/{BASE_COPERNICUS}/{nome}/{nome}.tif"


def recortar(lat: float, lon: float, raio_graus: float = 0.08):
    """Recorte do MDE ao redor de um ponto.

    Devolve (matriz de altitudes, transform, bounds) ou None se o tile não
    existir — o Copernicus não cobre oceano, e estação litorânea pode cair
    numa célula sem tile.
    """
    try:
        import rasterio
        from rasterio.windows import from_bounds
    except ImportError as erro:
        raise ImportError(
            "Este módulo precisa do rasterio: `pip install rasterio`. "
            "O resto do projeto funciona sem ele."
        ) from erro

    caixa = (lon - raio_graus, lat - raio_graus, lon + raio_graus, lat + raio_graus)
    try:
        with rasterio.open(url_tile(lat, lon)) as fonte:
            janela = from_bounds(*caixa, fonte.transform)
            altitudes = fonte.read(1, window=janela).astype("float32")
            transform = fonte.window_transform(janela)
    except Exception:
        return None

    if altitudes.size == 0:
        return None
    # O Copernicus usa valores muito negativos para "sem dado".
    altitudes[altitudes < -1000] = np.nan
    return altitudes, transform, caixa


def declividade_media(lat: float, lon: float, raio_graus: float = 0.05) -> float | None:
    """Declividade média do terreno ao redor da estação, em m/m.

    Entra em Kirpich e serve de indicador de resposta: encosta íngreme
    concentra rápido, planície demora.
    """
    recorte = recortar(lat, lon, raio_graus)
    if recorte is None:
        return None
    altitudes, _, _ = recorte
    if np.isnan(altitudes).all():
        return None

    passo_lat = METROS_POR_GRAU_LAT * (2 * raio_graus) / max(altitudes.shape[0], 1)
    passo_lon = (
        111320.0 * math.cos(math.radians(lat)) * (2 * raio_graus)
        / max(altitudes.shape[1], 1)
    )
    dy, dx = np.gradient(altitudes, passo_lat, passo_lon)
    inclinacao = np.sqrt(dx ** 2 + dy ** 2)
    return float(np.nanmean(inclinacao))


def perfil_do_vale(lat: float, lon: float, raio_graus: float = 0.05) -> dict | None:
    """Como o terreno sobe a partir do ponto mais baixo da vizinhança.

    É o que permite converter cota em área alagada: para cada altura acima do
    talvegue, quantos pixels do recorte ficariam submersos.
    """
    recorte = recortar(lat, lon, raio_graus)
    if recorte is None:
        return None
    altitudes, _, caixa = recorte
    validos = altitudes[~np.isnan(altitudes)]
    if validos.size < 100:
        return None

    # O talvegue é o fundo do vale. Usamos o percentil 1 em vez do mínimo
    # absoluto para não ancorar num pixel espúrio.
    fundo = float(np.percentile(validos, 1))

    area_pixel_m2 = (
        METROS_POR_GRAU_LAT * (2 * raio_graus) / max(altitudes.shape[0], 1)
    ) * (
        111320.0 * math.cos(math.radians(lat)) * (2 * raio_graus)
        / max(altitudes.shape[1], 1)
    )

    curva = {}
    for altura_m in (1, 2, 3, 5, 7, 10, 15, 20, 30):
        submersos = int(np.nansum(altitudes <= fundo + altura_m))
        curva[altura_m] = round(submersos * area_pixel_m2 / 1e6, 3)  # km²

    return {
        "altitude_talvegue_m": round(fundo, 1),
        "altitude_maxima_m": round(float(np.nanmax(altitudes)), 1),
        "amplitude_m": round(float(np.nanmax(altitudes)) - fundo, 1),
        "area_alagada_km2_por_altura": curva,
        "area_analisada_km2": round(validos.size * area_pixel_m2 / 1e6, 1),
    }


def mancha_por_cota(
    lat: float, lon: float, cota_cm: float, raio_graus: float = 0.05,
    cota_referencia_cm: float | None = None,
) -> dict | None:
    """Área inundada projetando a cota sobre o terreno real.

    Diferente da faixa de largura estimada: aqui a extensão vem da topografia.
    Onde o terreno é encaixado a água sobe sem espalhar; onde é plano, um
    metro a mais alaga quilômetros.

    A ÂNCORA IMPORTA MAIS QUE O RESTO. A régua marca zero num datum próprio da
    estação, muito acima do fundo do vale — em Lajeado, 18,5 m acima. Tratar a
    cota como altura sobre o talvegue inundou 4,7 vezes a área real. Passe
    `cota_referencia_cm` com a cota de inundação oficial: por definição é o
    nível em que o alagamento começa, o que fixa o zero corretamente.

    VALIDADO contra a modelagem hidráulica do IPH-UFRGS em Lajeado, ancorando
    na cota de inundação (1.850 cm):

        cota      oficial    MDE     razão
        1.900 cm   3,65      4,03    1,10x
        2.200 cm   5,62      5,78    1,03x
        2.500 cm   9,80      7,15    0,73x
        2.800 cm  15,41     10,31    0,67x
        3.100 cm  18,33     17,01    0,93x
                          média      0,89x

    Dentro de ±35 % da modelagem hidráulica, com preenchimento por altura sobre
    MDE de 30 m. Sem a âncora seriam 4,72x.

    RESSALVA HONESTA: isto é preenchimento por altura ("bathtub fill"), não
    modelo hidráulico. Não considera velocidade, rugosidade, diques nem
    conectividade hidráulica — uma depressão isolada no meio do terreno entra
    como alagada, ainda que a água não tenha por onde chegar nela. É melhor que
    largura proporcional, e é pior que a modelagem do IPH-UFRGS: onde houver
    mancha oficial, use a oficial.
    """
    recorte = recortar(lat, lon, raio_graus)
    if recorte is None:
        return None
    altitudes, transform, caixa = recorte
    validos = altitudes[~np.isnan(altitudes)]
    if validos.size < 100:
        return None

    fundo = float(np.percentile(validos, 1))
    # A régua marca zero num datum próprio da estação. Sem esse zero, a
    # referência é o fundo do vale — o que torna o resultado RELATIVO: serve
    # para comparar cotas entre si, não para dizer altitude absoluta da lâmina.
    base_cm = cota_referencia_cm if cota_referencia_cm is not None else 0.0
    lamina_m = max(0.0, (float(cota_cm) - base_cm) / 100.0)

    mascara = altitudes <= (fundo + lamina_m)

    area_pixel_m2 = (
        METROS_POR_GRAU_LAT * (2 * raio_graus) / max(altitudes.shape[0], 1)
    ) * (
        111320.0 * math.cos(math.radians(lat)) * (2 * raio_graus)
        / max(altitudes.shape[1], 1)
    )

    return {
        "cota_cm": float(cota_cm),
        "lamina_m": round(lamina_m, 2),
        "altitude_talvegue_m": round(fundo, 1),
        "area_alagada_km2": round(int(np.nansum(mascara)) * area_pixel_m2 / 1e6, 3),
        "mascara": mascara,
        "transform": transform,
        "bounds": caixa,
        "metodo": "preenchimento por altura sobre MDE Copernicus 30 m",
    }


def poligonos_da_mancha(resultado: dict, simplificar_graus: float = 0.0003) -> dict:
    """Converte a máscara de pixels em GeoJSON, pronto para o folium."""
    from rasterio.features import shapes
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union

    mascara = resultado["mascara"].astype("uint8")
    geoms = [
        shape(g) for g, v in shapes(mascara, mask=mascara.astype(bool),
                                    transform=resultado["transform"]) if v == 1
    ]
    if not geoms:
        return {"type": "FeatureCollection", "features": []}

    unido = unary_union(geoms).simplify(simplificar_graus, preserve_topology=True)
    return {
        "type": "Feature",
        "properties": {
            "cota_cm": resultado["cota_cm"],
            "area_km2": resultado["area_alagada_km2"],
            "metodo": resultado["metodo"],
        },
        "geometry": mapping(unido),
    }


if __name__ == "__main__":
    import sys

    lat = float(sys.argv[1]) if len(sys.argv) > 2 else -29.2352
    lon = float(sys.argv[2]) if len(sys.argv) > 2 else -51.8551
    print(f"MDE ao redor de ({lat}, {lon}) — tile {_nome_tile(lat, lon)}")

    perfil = perfil_do_vale(lat, lon)
    if perfil:
        print(f"  talvegue {perfil['altitude_talvegue_m']} m | "
              f"máxima {perfil['altitude_maxima_m']} m | "
              f"amplitude {perfil['amplitude_m']} m")
        print("  área alagada por altura acima do talvegue:")
        for altura, area in perfil["area_alagada_km2_por_altura"].items():
            print(f"     {altura:>3} m -> {area:7.2f} km²")

    decl = declividade_media(lat, lon)
    if decl:
        print(f"  declividade média: {decl:.4f} m/m ({decl * 100:.1f} %)")
