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

### Cotas de referência: só o SACE publica

Das 59 estações analisáveis, **26 têm cota oficial e 33 não**. Dessas 33, cinco
são barragens (UHE/PCH/CGH), que não têm cota de inundação por natureza — sobram
**28 estações de rio sem limiar**, 21 delas da telemetria da ANA.

A ANA **tem** esses valores: o aplicativo dela emite alerta por cota de
referência. Mas não os publica em interface aberta. Procurado em quatro vias,
todas verificadas:

| Via | Resultado |
|---|---|
| `hidroweb/rest/api/estacaotelemetrica` | 401 — exige token |
| SOAP `ServiceANA.asmx` — as 12 operações | nenhuma devolve cota de referência |
| `ListaEstacoesTelemetricas` (3,8 MB, rede inteira) | 13 campos, nenhum de limiar |
| ArcGIS aberto `Estações_Hidrometeorológicas_SNIRH` | 73 campos; os `EscalaNivel*` são equipamento e data, não limiar |

Por isso essas estações aparecem como *"sem cota de referência publicada"* em vez
de receberem um limiar arbitrado. **Elas continuam tendo projeção** de quanto o
rio sobe e em quanto tempo — o que falta é só a tradução disso em nível de
alerta, que depende de um valor que a fonte não abre.

Achado lateral aproveitável: o ArcGIS aberto traz `AreaDrenagem` **por estação**,
mais preciso que a área da bacia inteira que o modelo agrupado usa hoje em
`log_area`.

## Arquivos

```
georisk_dados.py      motor: coleta real + banco SQLite + padronização
georisk_mapa.py       desenho do mapa, compartilhado pelos dois painéis
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

## Atualização automática e arquivo histórico

O workflow `.github/workflows/coleta.yml` roda de 3 em 3 horas, independente da
sua máquina, e versiona dois produtos em `dados/`:

| Arquivo | O que é | Resolução |
|---|---|---|
| `estacoes.csv` / `.json` | Retrato do estado atual das 565 estações | a cada 3 h |
| `serie/AAAA-MM.csv.gz` | **Arquivo histórico** da série medida | 15 min |

O banco SQLite não é commitado (binário, grande, reconstruível).

### Por que o arquivo mensal existe

O SACE publica uma **janela móvel de ~30 dias**. O que sai dessa janela some da
fonte. Antes, o banco apenas espelhava essa janela — cada coleta apagava a série
e regravava —, então a cheia de julho de 2026 deixaria de existir no projeto em
meados de agosto. Para um trabalho que analisa eventos meses depois, isso é
perda de dado primário.

Agora a série **acumula**: o `INSERT OR REPLACE` com chave
`(estação, grandeza, datahora)` já corrige valor revisado pela fonte sem
destruir o passado, e o purge foi removido.

O arquivo vive no Git, não só no banco, porque quem coleta com tudo fechado é o
runner do GitHub — que nasce vazio e é destruído a cada execução, sem banco para
acumular. O exportador só reescreve os meses presentes no banco, unindo com o
que já estava versionado; mês antigo não é tocado.

Verificado nos dois cenários: apagando julho do banco e reexportando, e rodando
a coleta num runner sem banco nenhum — as 200.095 linhas de julho sobreviveram
intactas nas duas vezes. Comprimido, o mês fica em 0,67 MB.

### Máquina nova

```bash
git clone https://github.com/thainasantosaluno/georisk-rs.git
pip install -r requirements.txt
python georisk_dados.py --importar-arquivo   # recupera o histórico versionado
python georisk_dados.py                      # coleta o estado atual
```

Sem o `--importar-arquivo` você teria só os 30 dias que a fonte publica hoje.

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

### Faixas sobre a hidrografia oficial — cobrindo o resto

Só 5 municípios têm mancha modelada, e as estações que mais importam hoje
(Estrela, Encantado, Muçum, Bom Retiro do Sul) não estão entre elas. Para essas,
o painel desenha **faixas ao longo do curso real do rio**, nas três cotas.

| | |
|---|---|
| **Real** | traçado, posição e nome do rio — hidrografia do IBGE `BC100_RS_2021`, 1:100.000 |
| **Real** | as cotas de atenção/alerta/inundação, oficiais do SACE |
| **Estimado** | a largura da faixa em cada cota |

A largura **não é mancha modelada**. Sem MDE e sem seção transversal não há como
derivar até onde a água chega; a faixa é proporcional à altura da cota e serve
para comparar os três níveis, não para dizer a extensão do alagamento. Onde
existe mancha oficial, ela tem prioridade.

Há dois modos, escolhidos na barra lateral:

**Envelope da projeção** (padrão) — a mancha é da **cota projetada** pelo
modelo, e as cores formam um gradiente de risco do rio para fora:

| Cor | Cota | Leitura |
|---|---|---|
| 🔴 **Extremo alerta** | projetada − erro | a água chega aqui mesmo no cenário otimista |
| 🟠 **Alerta** | projetada | projeção central do modelo |
| 🟡 **Atenção** | projetada + erro | pior caso dentro do erro observado |
| 🟢 **Seguro** | pior caso × 1,5 | além do alcance projetado, com margem |

O verde não é uma cota medida: é a área que a projeção **não** alcança, com 50 %
de folga sobre o limite superior do erro. É ele que dá sentido operacional ao
mapa — sem ele, quem está na borda do desenho não sabe se está fora do alcance
ou apenas fora do que foi desenhado. Vai quase transparente, só com o contorno,
porque sendo a maior de todas cobriria as faixas de risco por dentro.

A folga de 50 % é deliberada: errar chamando de perigoso o que é seguro custa
menos que o contrário.

A largura do envelope é o **erro típico que o modelo cometeu na validação
walk-forward** — não um intervalo de confiança formal, mas o que se pode
afirmar com os dados. Em Encantado no evento de julho, a projeção de +25 h saiu
em 1.397 cm com envelope de 1.256 a 1.537 cm (±140 cm).

**Limiares fixos** — as três cotas oficiais de atenção, alerta e inundação,
independentes da projeção.

Resultado: as 44 estações com cota oficial passam a ter as três cores —
**nenhuma fica sem cobertura**.

Duas medidas foram necessárias para isso ser usável. As faixas brutas davam
1 MB de GeoJSON por estação, 44 MB no HTML, e o navegador travava antes de
desenhar; simplificar a ~55 m de tolerância (irrelevante numa faixa de centenas
de metros) reduziu para 26 KB por estação. E a busca traz ~120 trechos num raio
de 11 km, o que desenhava toda a rede de afluentes — restringir a 5 km deixa a
faixa reconhecível.

O traçado esquemático de senos e cossenos continua existindo como opção
desligada, para comparação.

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

## Encharcamento do solo e volume

Duas coisas que a lâmina de chuva sozinha não conta.

### Encharcamento — o que transforma chuva normal em cheia

O estado de umidade do solo deixou de ser um interruptor de três posições
(AMC I/II/III pela chuva de 72 h) e virou índice contínuo:

    API_t = k · API_{t-1} + P_t          k = 0,5^(passo/meia-vida)

A chuva de hoje soma, a de ontem ainda pesa menos, a da semana passada quase
não pesa. Meia-vida padrão de 3 dias.

O degrau de 72 h tinha dois defeitos. Era descontínuo — 52 mm e 54 mm davam CN
muito diferentes com o solo praticamente no mesmo estado. E ignorava *quando* a
chuva caiu: 60 mm concentrados ontem encharcam muito mais que 60 mm espalhados
em três dias, e a soma de 72 h não distingue.

O efeito é grande. Encantado, mesma bacia e mesmo CN base de 73,3:

| | Cheia de 22/07 | Período seco |
|---|---|---|
| Encharcamento (API) | 88,1 mm — saturação **100 %** | 18,5 mm — saturação **0 %** |
| CN ajustado | **86,3** | **53,6** |
| Chuva 24 h | 68,8 mm | 1,4 mm |
| **Coeficiente de escoamento** | **0,531** | **0,000** |

Com o solo saturado, metade da chuva vira vazão. Com o solo seco, tudo
infiltra. É a saturação prévia — não o total de chuva — que decide.

### A bacia armada — por que a segunda chuva alaga

Uma vez saturado, o solo leva dias para voltar a absorver. Nesse intervalo,
qualquer chuva nova escoa quase inteira. Encantado em julho de 2026 é caso de
manual:

| Dia | Chuva 24 h | Saturação | Cota |
|---|---|---|---|
| 20/07 | 35,8 mm | 2 % | **138 cm** |
| 21/07 | 69,6 mm | **100 %** | **771 cm** |
| 22/07 | 70,4 mm | **100 %** | **1.708 cm** |

A chuva do dia 20 quase não moveu o rio — foi gasta encharcando o solo. Do dia
21 para o 22 choveu praticamente o mesmo (69,6 → 70,4 mm) e a cota **mais que
dobrou**, porque já não havia absorção.

### O encharcamento não aparece — ele age

Decisão de projeto: o estado do solo **não é exibido** em lugar nenhum. Sem
aviso escrito, sem marcação no gráfico, sem indicador. Ele atua onde muda o
resultado:

- modula o CN de forma contínua, alterando retenção, abstração inicial e
  portanto a chuva efetiva;
- entra como preditor candidato, e a seleção por validação decide se fica;
- define o volume escoado do balanço.

As estimativas já saem considerando o solo. Com 50 mm de chuva no Taquari-Antas:

| Estado do solo | API | CN | Escoa | Volume |
|---|---|---|---|---|
| Seco | 0 | 53,6 | 0,2 mm | 4 milhões m³ |
| Meio do caminho | 44 | 69,9 | 5,8 mm | 152 milhões m³ |
| Encharcado | ≥ 53 | 86,3 | 21,4 mm | **564 milhões m³** |

A mesma chuva, cem vezes mais escoamento. `avaliar_vulnerabilidade()` continua
disponível para consulta por código — `resposta["vulnerabilidade"]` traz grau de
saturação, horas até dessaturar e simulações — mas nada disso vai para a tela.

### A seleção decide durante o evento, não antes

O conjunto de preditores **não é fixo**. Ele é reescolhido a cada estimativa,
testando seis candidatos fora da amostra. A prova de que isso responde ao evento
em tempo real: travando o modelo em três instantes da cheia de julho de 2026 com
`ate_instante`, sem deixá-lo ver nada do futuro.

| instante | API | cota Estrela | conjunto escolhido |
|---|---|---|---|
| 20/07 12 h | 35 mm | 1.302 cm | somente cota |
| 21/07 12 h | 64 mm | 1.339 cm | somente cota |
| 22/07 12 h | **95 mm** | 2.270 cm | **cota + chuva + CN + montante** |

Nas quatro estações do Taquari testadas (Estrela, Encantado, Muçum, Bom Retiro
do Sul) o bloco de chuva + CN entrou **sozinho** no dia 22, quando o solo
saturou. Em Bom Retiro o encharcamento entrou até como variável explícita. No
dia 20, solo seco, o mesmo código havia descartado tudo isso.

Hoje, fora de evento, o API está em 33–52 mm nas 22 estações — praticamente
constante. Variável sem variância não explica nada, e a validação corretamente a
descarta: o bloco de chuva + CN vence em 9 das 22. Não é o método falhando, é
ele se recusando a usar informação que no momento não existe.

**O solo age mesmo quando não aparece na lista.** O API alimenta
`_ajustar_cn_por_umidade(cn_base, p72_mm, api_mm)`, que é chamada dentro de
`calcular_chuva_efetiva`. Quando o conjunto vencedor inclui "CN", a saturação
**já está** dentro de `pe_mm` — é ela que decide se 68 mm viram 53 % de
escoamento ou zero. A coluna `encharcamento` separada só entra quando acrescenta
algo além disso, o que é raro porque durante evento ela é colinear com o p72h.

### Volume, não só lâmina

Milímetro não diz quanta água é. `mm × km² × 1.000` dá metros cúbicos, grandeza
comparável com a vazão do rio e com o que a calha comporta. Os mesmos 40 mm
significam coisas diferentes no Caí (4.956 km²) e no Uruguai (215.612 km²).

No pico de 22/07, sobre os 26.315 km² do Taquari-Antas:

| | Volume |
|---|---|
| Precipitado | 1.810 milhões de m³ |
| **Escoado** (virou vazão) | **962 milhões de m³** |
| Infiltrado | 849 milhões de m³ |

O balanço traz `volume_precipitado_m3`, `volume_escoado_m3`,
`volume_infiltrado_m3` e `coeficiente_escoamento`.

## Série histórica — anos de dado para calibrar e validar

A telemetria devolve dias e o SACE publica 30. Com isso o projeto tinha **um**
evento de cheia para trabalhar, e qualquer calibração viraria decorar aquele
caso.

`HidroSerieHistorica`, do mesmo serviço da ANA, resolve: série **diária**
consistida com mais de 15 anos. Em Encantado, 5.693 dias entre 2010 e 2025 —
97 % de cobertura — e **11 anos com pelo menos uma cheia**:

| Data | Pico |
|---|---|
| 02/05/2024 | **2.314 cm** |
| 04/09/2023 | 2.227 cm |
| 18/11/2023 | 2.061 cm |
| 08/07/2020 | 2.022 cm |
| 21/07/2011 | 1.902 cm |

```bash
python georisk_dados.py --historico --anos 15
```

Traz **156.877 dias de 34 estações**, de setembro/2011 a março/2026.

### Quais estações têm histórico — e por quê

Nenhuma das ~500 estações telemétricas do banco tem série histórica: são pontos
de monitoramento de usina (UHE/PCH/CGH), obras recentes sem registro longo.
Testei 12 e todas voltaram vazias.

Quem tem década de dado são as fluviométricas tradicionais da Rede
Hidrometeorológica Nacional — que são justamente as que o SACE acompanha e para
as quais existe cota oficial. O código do SACE é o da ANA truncado
(`8672000` → `86720000`), e o coletor tenta as variantes de preenchimento.

`catalogo_de_cheias()` monta a lista de eventos: em Encantado são **17 cheias
acima da cota de inundação** desde 2011.

### O formato tem uma armadilha

Para cada mês a ANA devolve **três séries**, distinguidas pela hora do
`DataHora` e pelo campo `MediaDiaria`: média diária às 00:00, leitura das 07 h e
leitura das 17 h. Empilhá-las como se fossem a mesma coisa dá três valores por
dia com significados diferentes.

E a escolha entre elas muda o resultado: o pico de 2.314 cm de 02/05/2024
aparece **só na leitura das 17 h** — a série de média diária nem tem valor para
aquele dia. Usar apenas a média perderia o pico, que é exatamente o que
interessa em análise de cheia. Por isso `serie_historica_ana()` devolve as três
colunas separadas e `valor` = máximo do dia.

### Validação da metodologia contra 15 anos de dado

Rodada em 10 estações da bacia do Taquari-Antas, ~5.000 dias cada, chuva de
Auler e Guaporé, subida diária da cota como alvo.

**Primeiro achado: a resposta é no MESMO dia.** Correlacionando a chuva com a
subida em diferentes defasagens em Encantado:

| Defasagem | Chuva bruta | Chuva efetiva |
|---|---|---|
| **mesmo dia** | 0,489 | **0,515** |
| dia seguinte | 0,142 | 0,034 |
| 2 dias | −0,217 | −0,262 |

Coerente com o Tc de ~12 h da estação. Uma primeira versão deste teste mediu no
dia seguinte e concluiu, erradamente, que o método piorava tudo.

**Segundo achado: o método separa cheia de não-cheia.** Nos 31 dias de cheia
contra os dias normais em Encantado:

| | Dias de cheia | Dias normais | Razão |
|---|---|---|---|
| Chuva bruta | 60,6 mm | 5,0 mm | 12× |
| **Chuva efetiva** | 21,1 mm | 0,48 mm | **44×** |

A chuva efetiva discrimina quase quatro vezes melhor que a lâmina bruta.

**Terceiro achado, e o mais desconfortável: na média das 10 estações, o SCS-CN
não supera a chuva bruta.**

| | Média das 10 estações |
|---|---|
| Chuva bruta | **0,485** |
| Chuva efetiva, CN fixo | 0,454 |
| + encharcamento (literatura) | 0,476 |
| + encharcamento calibrado | 0,478 |

O encharcamento recupera parte do que o CN fixo perde (+0,022), mas o conjunto
ainda fica abaixo da chuva bruta na correlação linear com a subida diária.

Em 3 das 10 estações o método ganha (Encantado 0,489→0,549; Lajeado
0,507→0,524; 86472000 0,551→0,611); nas outras 7, perde.

**A calibração dos limiares não se justifica.** O ótimo em Encantado
(meia-vida 30 dias, saturação 350 mm) rende +0,029 lá, mas na média das 10
estações o ganho sobre os valores de literatura é de +0,002 — ruído. Os
parâmetros seguem nos valores da literatura, que ao menos têm respaldo.

**O teste diário era injusto.** Duas objeções: dois postos de chuva para uma
bacia de 26.000 km², e resolução diária para um rio que sobe em 12 h. Refeito
com a série de 15 min e a chuva média de **14 postos**, em 12 estações do
Taquari:

| Método | r médio |
|---|---|
| Chuva bruta | 0,527 |
| + SCS-CN | 0,540 |
| + encharcamento | **0,543** |

O método passa a **ganhar**: de −0,031 no teste diário para **+0,016**. Vence
em 9 das 12 estações; perde em Bom Retiro do Sul, Porto Mariante e Taquari —
as três mais a jusante, onde o nível é governado pela onda que vem de montante,
não pela chuva sobre a bacia.

**Leitura final.** A metodologia se sustenta, mas o ganho sobre a chuva bruta é
modesto: cerca de 3 % na correlação. O que realmente separa cheia de não-cheia
é a razão de 44× no volume efetivo — e é para isso que o SCS-CN foi feito,
estimar volume de escoamento de evento, não maximizar correlação ponto a ponto.

Nada disso justifica calibrar os limiares: o ganho do encharcamento sobre o CN
fixo é de +0,002 nesta janela de 30 dias com um único evento. Com mais eventos
no arquivo, o teste ganha poder e a conclusão pode mudar.

### O que isso permite — e o que não permite

**Permite** calibrar os limiares de encharcamento contra dezenas de cheias
reais, aferir o CN e validar o método fora do período coletado.

**Não permite** calibrar o tempo de resposta (Tc): é dado diário, e Tc de 1 a
17 h exige resolução sub-diária. Para isso continua valendo o arquivo de 15 min
que a coleta acumula.

## Geologia, estrutura e relevo

Três camadas do BDIA/IBGE caracterizam cada bacia, com papéis deliberadamente
diferentes:

| Camada | Uso | Entra no cálculo? |
|---|---|---|
| `BDIA:geol_area` — litologia | Refina o grupo hidrológico **onde o solo é raso** | **Sim** |
| `BDIA:geom_area` — densidade de drenagem | Afere o Tc obtido por correlação | Não — é conferência |
| `geol_linha_falha` / `geol_linha_fratura` | Densidade de lineamentos (km/km²) | Não — é caracterização |

Nada disso aparece como painel: o dado alimenta o cálculo em silêncio. A
preparação roda junto com `python georisk_geo.py`.

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

## Relevo (MDE)

`georisk_relevo.py` usa o **Copernicus DEM GLO-30** (30 m, aberto, sem
autenticação). São COG, então dá para ler **só a janela de interesse** por
`/vsicurl/` — recortar a vizinhança de uma estação leva ~3 s, sem baixar o tile
de 49 MB.

```bash
python georisk_relevo.py -29.2352 -51.8551
```

Em Encantado: talvegue a 28,5 m, máxima 564 m, declividade média 25,6 %. E a
curva de área alagada por altura mostra a não linearidade que torna a cheia
catastrófica — de 10 m para 20 m acima do talvegue a área **triplica**, de
4,2 para 15,1 km², porque o vale se abre.

### A âncora do datum, e por que ela decide tudo

A régua marca zero num datum próprio da estação, muito acima do fundo do vale —
em Lajeado, 18,5 m acima. Tratar a cota como altura sobre o talvegue inundou
**4,7 vezes** a área real.

Passando a cota de inundação oficial como referência — por definição, o nível em
que o alagamento começa — o resultado bate com a modelagem hidráulica do
IPH-UFRGS:

| Cota | Oficial | MDE | Razão |
|---|---|---|---|
| 1.900 cm | 3,65 km² | 4,03 | 1,10× |
| 2.200 cm | 5,62 km² | 5,78 | 1,03× |
| 2.500 cm | 9,80 km² | 7,15 | 0,73× |
| 2.800 cm | 15,41 km² | 10,31 | 0,67× |
| 3.100 cm | 18,33 km² | 17,01 | 0,93× |
| | | **média** | **0,89×** |

Dentro de ±35 % da modelagem hidráulica. É preenchimento por altura, não modelo
hidráulico: não considera velocidade, rugosidade, diques nem conectividade — uma
depressão isolada entra como alagada ainda que a água não tenha por onde chegar.
Melhor que largura proporcional, pior que o IPH. Onde houver mancha oficial,
use a oficial.

## Qual chuva comanda o rio

O módulo correlacionava o nível só com a chuva medida **na própria estação** e,
onde ela era fraca, declarava "não use para decisão". O problema estava no
preditor, não na estação — e usar o preditor errado inutilizava a ferramenta
justamente nos rios grandes, que são os que mais importam.

Medido nas 22 estações do SACE com série de cota:

| Preditor | r médio | Fortes (> 0,30) |
|---|---|---|
| Chuva medida na estação | 0,302 | 17 de 22 |
| **Chuva média da bacia** | **0,540** | **20 de 22** |

Faz sentido físico: o rio integra a chuva de toda a área que drena para ele,
não a do pluviômetro que por acaso fica ao lado da régua. Passo Carreiro sai de
0,08 para 0,24; Taquari, de 0,53 para 0,65.

`escolher_chuva()` testa as duas e fica com a de maior correlação — critério
medido, não presumido — e `origem_chuva` registra qual foi usada. Das 22
estações, 14 se explicam melhor pela chuva da bacia e 8 pela local.

Duas seguem fracas mesmo assim: **Passo Carreiro** (0,07) e **Passo São Borja**
(0,14). Para essas a projeção vem do modelo agrupado, e isso fica dito no
`origem_projecao` em vez de mascarado.

## Confiabilidade da projeção

O painel dizia "projeção sem confiabilidade estatística" para quase todas as
estações — e estava sendo pessimista sem razão. O campo `confiavel` só olhava o
modelo treinado naquela estação sozinha, que de fato falha onde a chuva local
não explica o nível (em Passo Carreiro, r = 0,08). Mas o **modelo agrupado**,
validado com deixa-uma-estação-de-fora, tinha ganho **+0,52** prevendo estação
que nunca viu — e ficava relegado a uma nota de rodapé, enquanto o painel
exibia a projeção ruim da estação isolada.

Agora o agrupado **assume a projeção** quando o modelo local não supera a
persistência: a série de horizontes, o pico, a tendência e o tempo até cada
cota passam a vir dele, e `origem_projecao` diz qual dos dois respondeu.

Resultado nas 25 estações com série de cota:

| | |
|---|---|
| Com confiabilidade | **22 de 25 — 88 %** |
| pelo modelo da própria estação | 8 |
| pelo modelo agrupado | 14 |

As 3 restantes e as 12 sem série não têm projeção porque **a fonte não publica
o CSV de cota** para elas — limitação de origem, verificada estação por
estação, não defeito do cálculo.

### O mapa colore pela projeção, não pelo nível de agora

A aba de previsão tem mapa de satélite com as estações analisáveis, e a cor
responde "vai subir?" em vez de "está alto?". Isso exigiu tirar o cálculo da
interação: projetar uma estação custa ~1,2 s e as 59 levam **1,2 min** — tempo
demais para um rerun do Streamlit.

A projeção roda no coletor (`--projetar`), grava em `projecao_cache` e sai
versionada em `dados/projecao.csv`. O painel só lê, e mostra sempre a idade do
cache. O CSV commitado torna a projeção **auditável depois do fato**: dá para
conferir o que o sistema previa às 09 h UTC contra o que o rio realmente fez.

A classificação tem duas camadas, na mesma hierarquia das manchas:

| Camada | Quando vale | Regra |
|---|---|---|
| **Oficial** | 26 das 59 estações | maior cota do SACE que o pico projetado alcança |
| **Amplitude recente** | as outras 33 | subida projetada como fração de `p95 − p05` da janela de 30 dias |

A segunda camada existe porque não há alternativa: só 5 estações têm histórico
longo o bastante para um percentil, e sem cota oficial não há limiar. Ela
responde "vai subir muito para o que este rio costuma fazer?", que **não é** a
mesma pergunta que "vai extravasar" — e o painel diz isso na legenda. Onde a
janela de 30 dias não contém evento, a amplitude é pequena e a classe exagera.
Limitação declarada, não estimativa disfarçada.

### O slider de CN sobrescrevia o dado

O painel tinha um controle de Curve Number de 40 a 95. O módulo hidrológico já
resolve o CN **real** da bacia quando recebe `cn_base=None` — pedologia e uso da
terra do IBGE, refinados pela litologia — e o slider passava por cima disso.
Passo Carreiro estava sendo analisado com CN 52 porque era onde o controle
estava, quando a bacia dele tem **73,3**.

O controle saiu. O CN vem do dado e aparece no balanço com a origem declarada.

## Integridade do cadastro

O total de estações inflava a cada coleta — 565, 602, 654 — por dois motivos
que se somavam.

**Duplicatas por chave instável.** O id era `SACE_{bacia}_{pm}_{s}_{sr}`, mas
`s` e `sr` identificam séries do gráfico, não a estação, e o SGB os renumerou.
A mesma estação passou a chegar com id novo e a antiga ficou: 55 coordenadas
com estação repetida, Porto Mauá gravado três vezes. O `pm` é estável, e a
chave passou a ser só `SACE_{bacia}_{pm}`.

**Duplicatas entre bacias.** A página do Guaíba reexibe estações do Taquari e
do Caí, porque esses rios drenam para lá. A mesma estação física chega duas
vezes, e as cópias não são idênticas: a coordenada difere uns 30 m e o código
oficial vem em formatos distintos — Santa Tereza como `8647260` e `86472600`,
Vacaria como `2850045` e `02850045`. A cópia da página agregadora vem sem
leitura. Fica a que tem dado; no empate, a da bacia específica, que é a que
carrega as cotas oficiais.

**Órfãs nunca removidas.** `INSERT OR REPLACE` só insere e atualiza. Estação que
some da fonte, ou que muda de id, ficava para sempre no banco com a leitura
velha, aparecendo no mapa como se fosse atual — eram 78.

A limpeza tem guarda: só apaga órfãs da fonte que trouxe um número plausível de
estações naquela rodada. Sem isso, a falha silenciosa do SACE em agosto teria
apagado todas as estações dele.

Resultado: **551 estações, zero duplicatas, zero órfãs**.

## Robustez

O projeto roda sem nenhum aviso: `pyflakes` zerado nos seis módulos, nenhum
warning de runtime, nenhuma depreciação de biblioteca, e os dois painéis sobem
sem traceback.

Mas ausência de erro não é evidência de acerto — as falhas mais graves deste
projeto nunca produziram traceback. O que segue é o registro do que foi de fato
medido, contra referência independente sempre que houve uma.

### Registro de validação

| O que | Como foi testado | Resultado |
|---|---|---|
| Tempo de concentração | ordenação montante→jusante no Taquari | Sta. Tereza 9,75 h → Muçum 11,5 h → Encantado 12,25 h → Estrela 17 h — **fisicamente coerente** |
| Tempo de viagem | soma dos trechos vs medição direta Sta. Tereza→Taquari | 13,25 h somado vs **14,25 h medido — 7 %** |
| Propagação de onda | correlação entre estações vizinhas | **r = 0,58 a 0,81** |
| Qual chuva comanda | chuva local vs nível de montante, prevendo Estrela | local r = 0,08; montante r = 0,65 — **8× melhor** |
| Mancha por MDE | 5 cotas contra modelagem hidráulica IPH-UFRGS, Lajeado | média **0,89×** do oficial, dentro de ±35 % |
| Modelo agrupado | deixa-uma-estação-de-fora | ganho **+0,47 a +0,52** prevendo estação nunca vista |
| Magnitude projetada | retroteste da cheia de julho, Estrela (pico 2.477 cm) | erros **+154, +103, +28, −8 cm** nos 4 primeiros horizontes |
| SCS-CN | razão de volume efetivo cheia vs estiagem | **44×**, contra 12× da chuva bruta |
| Acumulado de chuva | contra o valor de 24 h publicado pelo próprio SACE | **bate exato** |
| Seleção de preditores | travada em 3 instantes do evento com `ate_instante` | mudou sozinha em **4 de 4 estações** ao saturar |
| Cobertura de projeção | 25 estações com série de cota | **22 confiáveis (88 %)** |

### Erros silenciosos corrigidos

Nenhum destes gerava exceção. Todos produziam número errado com aparência de
número certo — que é a falha perigosa num sistema de decisão.

| Sintoma | Causa | Correção |
|---|---|---|
| SACE devolvia zero estação, registrando *0 erros* | SGB trocou `L.marker` por `L.circleMarker`; situação migrou do ícone para a cor | as duas formas aceitas |
| Chuva de **25.281 mm** em 24 h | o CSV da fonte vinha preenchido com a série de nível | `_chuva_plausivel()` |
| Cota de **88.130 cm** | CGH/PCH da ANA publicam altitude absoluta, não régua | faixa física por grandeza, descarte registrado em `observacao` |
| Projeção de **7.781 cm** com o rio em 1.708 | modelo previa nível absoluto e extrapolava | prevê ΔH, recorta no envelope histórico |
| Mancha **4,72× maior** que a oficial | régua marca zero 18,5 m acima do talvegue | âncora na cota de inundação → 0,89× |
| "4,31 h até a inundação" com o rio já acima dela | limiar comparado sem checar o sentido | `JÁ ULTRAPASSADA` |
| Histórico com **16.101 dias** numa janela de 5.844 | três séries por mês somadas como se fossem uma | média diária, 07 h e 17 h separadas |
| Horizonte útil longo demais | pegava o máximo do ganho, que era ruído na cauda | exige sequência contígua |
| Calibração sem sinal | usava defasagem de 1 dia onde a resposta é no mesmo dia | defasagem corrigida |
| Validação cruzada sem sentido | estações do Uruguai validadas com chuva do Taquari | restrita à mesma bacia |
| Navegador travando | GeoJSON de 44 MB | simplificação 55 m + raio 5 km → **1,1 MB** |
| Painel bloqueado 30 s | modo envelope carregava tudo antes de abrir | padrão no modo rápido |
| Valor nulo virando padrão | `valor or padrao` — NaN é verdadeiro em Python | helpers `_ou()`/`ou()` com `pd.isna` |
| Painéis divergindo entre si | 7 funções duplicadas, 58 linhas já diferentes | extraídas para `georisk_mapa.py` |
| Contagem de estações inflando | chave instável + duplicata entre bacias + órfãs | 565 → 602 → 654 → **551 estáveis** |

Três merecem o detalhe, porque explicam decisões de arquitetura:

**A fonte mudou e o coletor calou.** Em agosto de 2026 o SGB trocou o desenho
das estações de `L.marker([lat, lon], {icon: NomeDoStatus})` para
`L.circleMarker([lat, lon], {fillColor: "#00FF33", …})` — a situação saiu do
nome do ícone e foi para a cor. O coletor passou três dias devolvendo zero
estação do SACE e registrando *0 erros*, porque a página respondia
normalmente, só não casava mais com o padrão. As duas formas agora são
aceitas. O aviso de "fontes fora de sincronia" no painel foi o que denunciou.

**262 conexões de banco vazadas por execução.** `with sqlite3.connect(...)`
faz commit mas **não fecha** a conexão, e o projeto usava esse padrão em 38
lugares. Com o painel aberto por horas atualizando a cada 15 min, chegaria ao
limite de descritores do processo. `conectar()` virou gerenciador de contexto
que fecha de verdade.

**"Sem dado" ao lado de um gráfico cheio de dados.** Estação que parou de
transmitir tem `nivel_cm` nulo — o que está certo, não há leitura atual — mas o
boletim escrevia "Sem dado" logo acima da série com centenas de pontos. Agora
mostra a última leitura conhecida e há quanto tempo ela foi: três dias sem
transmitir é problema de sensor, não ausência de histórico.

## Validade para predição

O que o conjunto autoriza afirmar, separado por pergunta, com o grau de
sustentação de cada resposta.

| Pergunta | Validade | Base |
|---|---|---|
| **Quando** a água chega em cada estação | **Alta** | Tc coerente com a cascata física; tempos de viagem aditivos com 7 % de discrepância; propagação r = 0,58–0,81 |
| **Quanto** já choveu e quanto escoou | **Alta** — é medição | leitura direta da fonte, acumulado conferido contra o SACE; volume em m³ pelo SCS-CN com CN real por bacia |
| **Quanto vai subir** | **Média, e só até metade do Tc** | agrupado ganha +0,47/+0,52; erro típico **± 140 cm**; além disso o envelope cobre tudo e a projeção perde para a persistência |
| **Onde alaga** — 5 municípios | **Alta** | mancha modelada pelo SGB/IPH-UFRGS |
| **Onde alaga** — onde há cota oficial | **Média** | MDE ancorado, 0,89× do oficial, ±35 % — mas é preenchimento por altura, ignora conectividade hidráulica |
| **Onde alaga** — resto do estado | **Baixa** | largura proporcional à cota; é parâmetro, não modelo |

### A limitação que atravessa tudo

**Não há previsão meteorológica no sistema.** Tudo é reativo: trabalha com a
chuva que **já caiu**. Se está chovendo agora, o sistema sabe o acumulado até
este instante e projeta a resposta do rio a ele — não sabe se vai continuar
chovendo. O nowcast é extrapolação da taxa recente e está rotulado como tal.

Ampliar o horizonte exigiria acoplar previsão quantitativa de chuva. O INMET não
devolve leitura sem token e o CEMADEN não publica endpoint aberto — ambos
registrados em [Fontes](#fontes) com o motivo da recusa.

### O envelope faz parte da resposta

Encantado em 07/08, projeção a partir de 199 cm subindo:

| horizonte | projetado | envelope |
|---|---|---|
| 3,1 h | 224 cm | 138 – 311 |
| 6,1 h | 259 cm | 86 – 433 |
| 18,4 h | 316 cm | **4 – 629** |

Às 3 h o intervalo é ±87 cm e serve para decidir. Às 18 h vai de "o rio secou" a
"dobrou" — a projeção continua sendo impressa, e continua não sendo informação.
O alargamento é comportamento correto: é o modelo dizendo onde parar de confiar
nele. Por isso o horizonte útil termina na metade do Tc, e por isso o envelope
nunca é omitido do painel.

### Uma lacuna de dado, não de método

Encantado projeta +118 cm e o SACE **não publica** cota de atenção, alerta nem
inundação para ela. Sabe-se quanto e quando; não se sabe se é grave. Sem limiar
não há como converter centímetro em decisão — e isso é limitação da fonte, não
do cálculo.

## Aviso

O traçado esquemático que sobrou como opção — senos e cossenos ao redor da
estação — **não** é mancha medida. Use as manchas oficiais; o esquemático serve
só para dar noção de extensão onde não existe modelagem publicada.
