' ---------------------------------------------------------------------------
' GeoRisk-RS - executa a coleta SEM abrir janela
' ---------------------------------------------------------------------------
' A tarefa agendada apontava direto para coletar.cmd, e o Windows abria um
' console a cada 15 minutos. Fora o incomodo, isso quebrava a coleta: fechar a
' janela mata o processo, e a tarefa terminava em 0xC000013A -- que foi o que
' aconteceu nas execucoes de 15:51 e 10:45.
'
' O wscript com o segundo parametro 0 executa de verdade oculto, sem o piscar
' que `powershell -WindowStyle Hidden` ainda produz. O terceiro parametro True
' faz esperar o fim, para o Agendador registrar o codigo de saida real em vez
' de dar sucesso imediato.
' ---------------------------------------------------------------------------

Dim shell, codigo
Set shell = CreateObject("WScript.Shell")
codigo = shell.Run("""C:\projetos\georisk-rs\coletar.cmd""", 0, True)
WScript.Quit codigo
