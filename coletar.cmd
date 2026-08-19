@echo off
chcp 65001 > nul
REM ---------------------------------------------------------------------------
REM GeoRisk-RS - coleta local a cada 15 minutos
REM ---------------------------------------------------------------------------
REM Existe porque o GitHub Actions nao honra cron de alta frequencia. Pedindo
REM a cada 15 min ele entrega cerca de 1 hora, medido neste repositorio nos
REM runs 102 a 104, com intervalos de 58 e 69 minutos. Agendamento em
REM repositorio publico e despriorizado sob carga.
REM
REM Roda pelo Agendador do Windows, que e servico do sistema: independe do
REM VS Code, do Claude e de qualquer janela aberta. Com o computador ligado,
REM alimenta o banco local a cada 15 min de verdade.
REM
REM NAO faz commit nem push, de proposito. Quem versiona o snapshot e o
REM workflow do GitHub; se os dois publicassem, disputariam a mesma
REM referencia. Aqui a funcao e manter o banco local fresco para o painel.
REM
REM A opcao --sem-arquivo pula a reescrita dos .csv.gz mensais, binarios e
REM caros de regravar a cada 15 min.
REM
REM chcp 65001 evita acentuacao quebrada no log, porque o Python escreve
REM UTF-8 e o console usa a pagina de codigo ANSI por padrao.
REM ---------------------------------------------------------------------------

cd /d "C:\projetos\georisk-rs"

set "LOG=C:\projetos\georisk-rs\coleta_local.log"

echo.>> "%LOG%"
echo ===== %DATE% %TIME% =====>> "%LOG%"

python -X utf8 georisk_dados.py --exportar --sem-arquivo >> "%LOG%" 2>&1

if errorlevel 1 (
    echo FALHA na coleta>> "%LOG%"
    exit /b 1
)

echo OK>> "%LOG%"
exit /b 0
