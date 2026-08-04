# GeoRisk-RS — Monitoramento de Cheias do Rio Grande do Sul

Painel de monitoramento hidrometeorológico do RS construído **exclusivamente
sobre dados reais publicados pelos órgãos oficiais**. Não há catálogo fixo,
valor semeado nem número de exemplo em lugar nenhum: onde a fonte não publica,
o painel mostra "Sem dado" e o ponto fica cinza.

## Fontes

| Fonte | O que fornece | Situação |
|---|---|---|
| **SACE / SGB-CPRM** | Nível, **cotas oficiais** de atenção/alerta/inundação, situação e série real de 15 em 15 min (30 dias) | ✅ em uso |
| **ANA / Telemetria** | Estações automáticas do RS: nível, vazão e chuva | ✅ em uso |
| INMET | Lista de estações responde, mas os endpoints de leitura devolvem 204/404 sem token e o portal está atrás de proteção anti-bot | ❌ fora — sem leitura real, a estação não entra |
| CEMADEN | Sem endpoint público aberto | ❌ fora |

Abrangência: **somente Rio Grande do Sul**. O SACE monitora a bacia do Uruguai
inteira, então vinham estações de Santa Catarina junto; a UF de cada coordenada
é resolvida contra o cadastro telemétrico nacional da ANA (~5,2 mil estações,
usado como gazetteer) e o que não é RS é descartado.

## Arquivos

```
georisk_dados.py      motor: coleta real + banco SQLite + padronização
georisk_geo.py        manchas oficiais + CN real por bacia (solo e uso da terra)
georisk_hidrologia.py módulo hidrológico: Tc, SCS-CN e projeção de cota
main.py               painel "Sala de Decisão"    (streamlit run main.py)
dashboard.py          painel "Sala de Situação"   (streamlit run dashboard.py)
dados/                snapshot padronizado publicado pela automação (CSV + JSON)
```

Os dois painéis compartilham o mesmo banco e o mesmo coletor — atualize por
qualquer um dos dois que o outro enxerga na hora.

## Como rodar

```bash
pip install -r requirements.txt
streamlit run main.py
```

Na primeira execução, clique em **Atualizar agora** na barra lateral. O coletor
leva ~40 s no modo rápido (só SACE) e ~2 min no modo completo (SACE + ANA).

Para coletar pela linha de comando:

```bash
python georisk_dados.py --exportar
```

## Formato único de saída

Toda estação, venha de qualquer fonte, passa pelo mesmo normalizador antes de
ser gravada. O contrato é sempre este:

| Campo | Unidade / domínio |
|---|---|
| `id` | `<FONTE>_<chave>` |
| `fonte` | `SACE/SGB` \| `ANA telemetria` |
| `lat` / `lon` | graus decimais WGS84, validados dentro do RS |
| `tipo` | `FLUVIOMETRICA` \| `PLUVIOMETRICA` |
| `nivel_cm`, `cota_*_cm` | centímetros |
| `chuva_1h/24h/72h` | milímetros acumulados |
| `vazao_m3s` | m³/s |
| `medido_em` | `YYYY-MM-DD HH:MM:SS` |
| `situacao` | vocabulário fechado (Normal, Cota de Atenção, …) |
| `cor` | `green` \| `gold` \| `orange` \| `red` \| `purple` \| `gray` |
| `observacao` | por que algum campo ficou vazio |

### Guardas de qualidade

As fontes oficiais às vezes publicam valor impossível. Em vez de repassar, o
coletor descarta e registra o motivo em `observacao`:

- alguns arquivos `*_chuva.csv` do SGB vêm preenchidos com a série de **nível**
  (somar aquilo dava "25.281 mm em 24 h");
- usinas CGH/PCH na telemetria da ANA transmitem **cota absoluta em metros**
  (ex.: 88.130 = 881,30 m em Vacaria), não régua fluviométrica em cm;
- o valor-sentinela `9999` significa "sem medição".

## Atualização automática

O workflow `.github/workflows/coleta.yml` roda de 3 em 3 horas, coleta das
fontes reais e versiona o snapshot em `dados/`. O banco SQLite não é commitado
(40+ MB, binário, reconstruível em ~2 min).

## Módulo hidrológico

`georisk_hidrologia.py` relaciona chuva e nível para estimar em quanto tempo
uma chuva chega ao rio. Está integrado na aba **Previsão Hidrológica** do
`dashboard.py` e roda também sozinho:

```bash
python georisk_hidrologia.py SACE_taquari_2_3_4
```

```python
import georisk_hidrologia as gh
r = gh.estimar_tempo_e_impacto_inundacao("SACE_taquari_2_3_4")
print(r["tempo_horas_ate_inundacao"], r["cota_maxima_projetada_cm"])
```

O que faz: tempo de resposta (Tc) por correlação cruzada entre a chuva de 15
min e a taxa de subida do rio; chuva efetiva por SCS-CN com umidade antecedente
tirada das 72 h; regressão da variação de cota (numpy puro — sem scikit-learn);
e tempo até cruzar as cotas oficiais do SACE.

### O que a validação mostrou

O Tc cresce de montante para jusante no Taquari-Antas (Santa Tereza 9,75 h →
Muçum 11,5 h → Encantado 12,25 h → Estrela 17 h), e cabeceiras pequenas
respondem em 1–2 h. No retroteste da cheia de 22/07 em Estrela (pico real
2.477 cm) os erros foram +154, +103, +28 e −8 cm nos quatro primeiros
horizontes.

Mas o ganho sobre a **persistência** ("o nível fica como está") só é positivo
na primeira metade do Tc: +0,46 em 4 h, +0,28 em 8,5 h, negativo de 12,75 h em
diante. Por isso o retorno traz `horizonte_util_horas` e `confiavel` — além
desse alcance, a projeção perde para não projetar nada.

Três limitações que valem conhecer: a chuva usada é a medida na própria
estação, então em rios grandes (baixo Uruguai, r≈0,03) a projeção não vale;
30 dias contêm poucos eventos para treinar; e Kirpich não é aplicado
automaticamente porque o banco não tem morfometria de bacia — a função existe
para quem tiver comprimento e declividade do talvegue.

## Camada geoespacial

`georisk_geo.py` traz geometria oficial e caracterização de solo. Prepare com:

```bash
python georisk_geo.py
```

### Manchas oficiais de inundação

| Fonte | Cobertura | Natureza |
|---|---|---|
| **SGB / IPH-UFRGS** | Lajeado, São Sebastião do Caí, Montenegro, Alegrete, Uruguaiana — 67 manchas | Indexadas por **cota em cm** |
| **Defesa Civil RS** | 7 municípios do Vale do Taquari | Evento de 22/07/2026 |

As do SGB encaixam direto no painel: como são indexadas por cota, escolher a
mancha é uma busca, não um cálculo. Montenegro medindo 765 cm exibe a mancha de
750 cm — a maior cota mapeada que não passa do nível medido, que é a leitura
conservadora (a área de 750 está contida na de 765).

A Defesa Civil publica 60 municípios, mas em PDF, sem geometria. Só os 7 do
Vale do Taquari têm mapa navegável, de onde sai KML.

A SEMA-RS não publica mancha consumível: o `geoportal.sema.rs.gov.br` está no
ar mas serve apenas a página padrão do IIS.

Onde não há mancha oficial, o painel diz isso — o traçado esquemático de senos
e cossenos virou opção desligada por padrão.

### Curve Number real por bacia

Antes o CN era 75 fixo para o estado inteiro. Agora vem de solo e uso da terra:

- `CREN:PedologiaSG22/SH21/SH22/SI22` (IBGE) → ordem e subordem do solo →
  grupo hidrológico A–D pela classificação de Sartori, Lombardi Neto & Genovez
- `CREN:Cobertura_uso_terra_2020_RS_serie_revisada` (IBGE) → classe de uso
- Polígono da bacia vindo do próprio SACE, o que evita delinear bacia por MDE

| Bacia | CN | Cobertura no RS | Solo identificado |
|---|---|---|---|
| Rio Uruguai | 74,7 | 57,4% | 94,1% |
| Guaíba | 73,8 | 100% | 96,5% |
| Rio Caí | 73,5 | 100% | 96,3% |
| Taquari-Antas | 72,3 | 100% | 98,2% |

O cálculo é por amostragem em grade regular dentro da bacia — que dá a mesma
média ponderada por área da interseção de polígonos, sem o custo e a fragilidade
de cruzar 48 mil polígonos. Pontos fora do RS são **descartados**, não
estimados: as bacias do Uruguai e do Taquari entram em SC, na Argentina e no
Uruguai, e a camada do IBGE é estadual.

Efeito no balanço hídrico: muda `retenção potencial`, `abstração inicial` e
`precipitação efetiva`. **Não** muda a precipitação acumulada, que é medição.
Em Encantado no pico de 22/07, o escoamento passou de 38,4 para 35,5 mm.

As duas tabelas de conversão (solo → grupo, uso × grupo → CN) continuam sendo
**parâmetro documentado, não medição**, e ficam expostas no topo do módulo.

## Geologia, estrutura e relevo

Três camadas do BDIA/IBGE caracterizam cada bacia, com papéis deliberadamente
diferentes:

| Camada | Uso | Entra no cálculo? |
|---|---|---|
| `BDIA:geol_area` — litologia | Refina o grupo hidrológico **onde o solo é raso** | **Sim** |
| `BDIA:geom_area` — densidade de drenagem | Afere o Tc obtido por correlação | Não — é conferência |
| `geol_linha_falha` / `geol_linha_fratura` | Densidade de lineamentos (km/km²) | Não — é caracterização |

**Litologia refinando o CN.** Neossolo Litólico e Cambissolo são rasos: ali a
rocha decide. Sobre basalto maciço da Serra Geral o escoamento é alto (grupo D);
sobre o arenito Botucatu a água infiltra (grupo A). Aplicar litologia em solo
profundo seria errado — lá quem manda é o solo. O refino reclassificou 734
pontos no Taquari (Cambissolo C→D) e o CN foi de 72,3 para 73,3.

**Densidade de drenagem aferindo o Tc.** Não realimenta o cálculo; só diz se o
Tc medido é compatível com o relevo. No Taquari (drenagem alta, esperado
1–10 h): Santa Tereza mede 9,75 h e sai *compatível*; Estrela mede 17,25 h e
sai *mais lento* — correto, porque estação de jusante soma o tempo de
propagação no canal, que a relação de drenagem não cobre.

**Lineamentos como caracterização.** Densidade de fraturamento é proxy
reconhecido de permeabilidade secundária, mas **não existe coeficiente
consagrado** que a converta em correção de Curve Number. Inventar esse fator
repetiria o que este projeto passou a remover. O índice é calculado e exibido;
a interpretação fica com quem tem referência da área.

| Bacia | CN | Drenagem | Lineamentos |
|---|---|---|---|
| Taquari-Antas | 73,3 | alta | 0,169 km/km² |
| Rio Caí | 74,8 | média | 0,175 km/km² |
| Rio Uruguai | 74,8 | média | 0,129 km/km² |
| Guaíba | 74,1 | alta | 0,154 km/km² |

## Aviso

O traçado esquemático que sobrou como opção — senos e cossenos ao redor da
estação — **não** é mancha medida. Use as manchas oficiais; o esquemático serve
só para dar noção de extensão onde não existe modelagem publicada.
