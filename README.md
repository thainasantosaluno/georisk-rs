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

## Aviso

As "manchas de inundação" do segundo painel são **traçado esquemático** gerado
matematicamente ao redor da estação — não são mancha de inundação medida ou
oficial. O que é real ali é a **cor**, que vem da cota medida comparada à cota
oficial do SACE. Para extensão de inundação oficial, integre uma camada WMS da
Defesa Civil / SGB.
