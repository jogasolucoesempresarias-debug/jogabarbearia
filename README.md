# JOGA Barbearia

SaaS de gestão para barbearia — **mobile-first + PWA**. Agenda interna, comanda (serviços + produtos),
planos de assinatura, comissão dos barbeiros, caixa, despesas/impostos e DRE.

Construído sobre a espinha do **Gestão JOGA** (auth, financeiro, deploy), como produto vertical da
JOGA Soluções Empresariais. **Instância própria por cliente** (banco/subdomínio próprios).

## Stack
Flask + Waitress · PostgreSQL · HTML/CSS/JS puro (mobile-first) + PWA · Docker Swarm/Traefik/GHCR.
Fuso horário **America/Sao_Paulo** (app via `TZ` e conexão do banco via `options`).

## Perfis (RBAC)
- **dono** — acesso total: agenda, comanda, caixa, despesas, comissões, DRE e configurações.
- **caixa/recepção** — operação: agenda, comanda, caixa, despesas, etc.
- **barbeiro** — **só** a própria agenda (somente leitura) + a própria comissão. Bloqueado de verdade
  no backend (`before_request`): não acessa caixa/financeiro/comandas/outros nem digitando a URL.
- **suporte/master (JOGA)** — login invisível por env (`SUPORTE_EMAIL`/`SUPORTE_SENHA`), fora do
  banco, role dono. Só existe se as envs estiverem setadas. Para configurar instâncias em produção.

## Conceitos
- **Comanda** é o pivô: ao fechar vira **receita no caixa**, registra a produção e respeita o **plano**
  (assinante atendido em dia coberto sai R$0; fora dos dias do plano vira cliente normal/avulso).
- **Comissão** (motor único em `calc_comissao`, usado pelo fechamento do dono e pela tela do barbeiro):
  - **Avulso**: 45% (config por barbeiro) sobre o valor do serviço executado.
  - **Assinante**: a regra é **do plano** (`planos.comissao_assinante_regra`), porque cada barbearia
    vende plano de um jeito. Um plano nunca cai em duas regras:
    - `bolo` — % (`config.comissao_padrao`) da arrecadação **dos planos 'bolo'** no período, rateado
      pela **produção** (1 visita = 1 atendimento, mesmo com vários serviços). Atribuição por comanda.
      A **dona não recebe comissão** (`recebe_comissao=false`); a fração dela fica com a casa, e a tela
      mostra **distribuído × retido**.
    - `tabela` — comissão normal do barbeiro sobre o **preço cheio** do serviço coberto, direto pra
      quem executou o item. O assinante paga R$0 e pro barbeiro é igual a um cliente avulso.
    - `fixo` — R$ por visita coberta, direto pra quem executou. Dois barbeiros na mesma visita
      contam uma cada.
    - `zero` — assinante não gera comissão.
  - **Fechar/pagar comissão** por barbeiro/quinzena → vira **despesa "Comissões" no caixa** e marca
    pago/pendente (tabela `comissoes_pagas`). Filtro por barbeiro + "fechar todos os pendentes".
    O **valor é editável** no fechamento: se diferir do calculado, o motivo é obrigatório e fica
    gravado (`valor_calculado` × `valor` × `ajuste_motivo`). O calculado vem do motor, nunca do front.
- **Planos de assinatura** (configuráveis: valor, serviços, dias, vencimento 10/30, múltiplos):
  na criação **cobra a 1ª mensalidade na hora** (cai no caixa hoje); a **próxima** vence no dia
  10/30 escolhido, **a partir do mês seguinte**; "Gerar cobranças do mês" cria as próximas como
  "a receber" (idempotente, via `proxima_cobranca`).
- **Despesas e Impostos**: lançamento com **categoria** (incl. Impostos), resumo por categoria, e
  **despesas fixas recorrentes** (aluguel/imposto/internet) com "Gerar despesas do mês".
- **Caixa**: fechamento do dia por forma de pagamento (Dinheiro/Pix/Cartão) + a receber/lançamentos.
- **DRE/Resultado**: receitas (serviços, produtos, assinaturas) − despesas (por categoria, incl.
  comissões pagas) = resultado. Período flexível (mês/trimestre/ano/intervalo), **evolução de 6
  meses** em gráfico, **drill-down** por serviço/produto (qtd + valor), ticket médio, nº de
  atendimentos e comparativo ▲/▼ com o período anterior.
- **Uso dos Planos**: documenta e analisa as visitas dos assinantes. **Registrar visita** em 1 passo
  (assinante + barbeiro executor + data, aceita retroativo) cria/fecha uma comanda R$0 — reusa a
  máquina de comissão (o barbeiro entra no bolo) sem distorcer o caixa. Painel por assinante:
  visitas no mês, última visita, frequência média, custo por visita, status de uso
  (intenso/regular/dormente/nunca), distribuição por barbeiro; agregados de margem dos planos e
  assinantes em risco de churn.
- **Agenda**: slots de 30min, multi-serviço (soma a duração), walk-in, bloqueio/folga, cancelamento
  sem taxa, horário por dia.
- **Cadastro de cliente**: telefone com máscara `(DD) 9XXXX-XXXX` e nome em MAIÚSCULO.
- **Onboarding de uma barbearia nova** (entrega assistida — o cliente não encara wizard nenhum):
  1. `/setup` (login dono/master) → **gera o link da ficha** com token.
  2. `/coleta?t=TOKEN` — página **pública**, mobile, salvamento automático. A barbearia preenche só
     o que só ela sabe: nome, equipe (quem é o dono, % de cada um, quem precisa de login), serviços
     com preço e duração, horário, produtos, planos e formas de pagamento. Tudo já vem no preset
     (`PRESET_FICHA`), ele edita em vez de criar. A pergunta da comissão do assinante aparece em
     linguagem de dono e vira a regra do plano.
  3. `/setup` de novo → resumo do que vai nascer, **importar/exportar JSON** (começar de outra
     barbearia pronta) e **Aplicar**: cria tudo numa transação só, gera os logins (senha
     `SEED_SENHA_INICIAL`, troca no 1º acesso) e devolve a lista pra você entregar.
  - Aplicar **roda uma vez**: é bloqueado se a ficha já foi aplicada ou se a instância já tem
    comandas/lançamentos (evita cadastro duplicado). Depois disso o token é anulado e `/coleta` fecha.
- **Hub de coleta** (`MODO_COLETA=1`, em `coletabarbearia.jogasolucoes.com.br`): mesma imagem, banco
  próprio, **não é barbearia nenhuma**. Serve pra mandar a ficha ainda na negociação sem abrir
  instância pra quem não fechou. Guarda **N fichas** (uma por prospect, cada uma com seu token e seu
  status); o `/setup` vira uma lista com "criar ficha", link, "copiar a ficha" e apagar. O **aplicar
  é bloqueado** nele. Fechou a venda → cria a instância do cliente, cola a ficha no `/setup` de lá e
  aplica. Stack em `docker-compose.coleta.yml`.
- **Instância de demonstração** (`MODO_DEMO=1`, em `demobarbearia.jogasolucoes.com.br`): barbearia
  fictícia ("Barbearia do Zé") que o prospect navega sozinho antes de preencher a ficha. A tela de
  login mostra dois acessos — **dono** (vê tudo) e **barbeiro** (vê só a própria agenda e comissão,
  o que demonstra o RBAC melhor do que explicar). `/setup` fica fechado lá. Populada por
  `seed_demo.py`, que **também é o reset**: apaga tudo e refaz, com **todas as datas relativas a
  hoje** (data fixa faria a demo parecer abandonada em dois meses). Cron sugerido:
  `0 4 * * * docker exec $(docker ps -q -f name=demobarbearia) python -X utf8 seed_demo.py`.
  Stack em `docker-compose.demo.yml`. **O seed se recusa a rodar sem `MODO_DEMO`** — ele apaga
  dados, e essa trava é o que impede o desastre de executá-lo na instância de um cliente.
- **Alerta de WhatsApp**: quando o prospect clica em *Enviar*, a JOGA recebe um aviso via **UazAPI**
  (`POST /send/text`, mesmo contrato do DanfeZap/diagnóstico) com o nome da barbearia, o tamanho da
  ficha e o link pra ver. Vai em **thread de fundo** — o cliente não espera rede, e falha de WhatsApp
  nunca derruba o salvamento. Salvar rascunho não alerta, só o envio. `ALERTAS_ATIVO` é a trava
  mestra: desligada, nada sai (é o que mantém dev e smokes quietos).
- **Sem cor por cliente**: o tema do app é fixo (`app.css`). `configuracoes.marca_cor` continua no
  banco por compatibilidade, mas **não é lida por nada** — o que existe de verdade é a `cor_agenda`
  de cada barbeiro, atribuída automaticamente de uma paleta pra distinguir as colunas da agenda.

## Variáveis de ambiente (`.env` / Portainer)
```
SECRET_KEY            # chave do Flask
DB_HOST/PORT/NAME/USER/PASSWORD
SEED_SENHA_INICIAL    # senha inicial dos usuários semeados (troca no 1º login)
SUPORTE_EMAIL         # acesso master JOGA (vazio = desativado)
SUPORTE_SENHA
MODO_COLETA           # 1 = hub de coleta (não é barbearia; guarda N fichas). Vazio = instância normal
ALERTAS_ATIVO         # 1 = dispara o WhatsApp de verdade. Vazio = só loga (dev/testes)
UAZAPI_URL            # https://free.uazapi.com
UAZAPI_TOKEN
ALERTA_WHATSAPP       # quem recebe o aviso, separado por vírgula
```

## Setup local (dev)
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# copie .env.example para .env e ajuste DB_PASSWORD (e SUPORTE_* se quiser testar o master)
.\.venv\Scripts\python.exe -X utf8 _create_db.py      # cria o database
.\.venv\Scripts\python.exe -X utf8 init_db.py         # schema (idempotente)
.\.venv\Scripts\python.exe -X utf8 seed_barbearia.py  # dados pré-configurados
$env:PORT="5002"; .\.venv\Scripts\python.exe -X utf8 server.py   # http://localhost:5002
```
**Logins** (senha `joga123`, troca no 1º acesso): `regiane@barbearia.local` (dona+barbeira, **sem
comissão**) · `joaovictor@barbearia.local` (barbeiro). `_reset.py` reseta o banco local (dev).

### Smoke tests (cada um limpa o resíduo no fim)
- `_smoke.py` — fluxo operacional (cliente, agenda, comanda, caixa, assinatura, comissão)
- `_smoke_pool.py` — rateio do bolo por visita
- `_smoke_gestao.py` — timezone, dona fora do rateio, fechar comissão→despesa, DRE
- `_smoke_despesas.py` — despesas, categorias, fixas (gerar do mês)
- `_smoke_assinatura.py` — 1ª no caixa hoje + próxima no dia 10/30 do mês seguinte
- `_smoke_uso.py` — registrar visita de assinante, Uso dos Planos e DRE aprimorado (test_client)
- `_smoke_regras.py` — as 4 regras de comissão do assinante + o ajuste manual no fechamento
- `_smoke_onboarding.py` — ficha → aplicar → barbearia nascida, **num banco descartável próprio**
  (cria/derruba `joga_barbearia_smoke` e sobe um servidor na 5003; não toca no banco de dev)
- `_smoke_hub.py` — hub `MODO_COLETA=1`: N fichas convivendo, token de uma não abre a outra,
  aplicar bloqueado (banco descartável próprio)
- `_smoke_alerta.py` — alerta de WhatsApp: sobe uma **UazAPI de mentira** local e confere número
  normalizado, header do token, texto e os 2 destinatários — sem gastar mensagem nem usar internet
- `_smoke_demo.py` — a trava do `seed_demo.py`, o reset sem duplicar, **a ficha de coleta
  sobrevivendo ao reset** e as telas que o prospect vê (~50s: roda o seed completo de propósito,
  porque "dormente" e o gráfico de 6 meses só existem na janela real de 180 dias)

> Os smokes que sobem servidor escolhem **porta livre pelo SO**. Porta fixa fazia o teste conversar
> com um servidor de dev já rodando e validar a instância errada em silêncio.
- `_teste_completo.py` — bateria ponta a ponta

## Deploy
`git push main` → GitHub Actions builda e publica no GHCR → no servidor:
```bash
docker service update --image ghcr.io/jogasolucoesempresarias-debug/jogabarbearia:latest --force barbearia_barbearia-app
docker exec $(docker ps -q -f name=barbearia) python -X utf8 init_db.py     # migrações são aditivas (IF NOT EXISTS)
```
Stack em `docker-compose.prod.yml` (Traefik → `barbearia.jogasolucoes.com.br`). Migrações nunca
destroem dado. Reinstalação limpa (teste): scale 0 → drop/create DB → scale 1 → init_db + seed.

## Estrutura
```
server.py            # backend: auth+RBAC, agenda, comanda, assinaturas, despesas, caixa, comissões, DRE
init_db.py           # schema (idempotente, migrações aditivas)
seed_barbearia.py    # dados pré-configurados (serviços, produtos, 2 barbeiros, horários, 3 planos)
static/app.css|js    # shell mobile-first (bottom-nav, máscara telefone, helpers)
agenda · comanda · clientes · assinaturas · uso(Uso dos Planos) · caixa · despesas · relatorios(Comissões) · dre · config · barbeiro .html
login.html · trocar-senha.html · cadastro.html(QR público)
coleta.html      # ficha pública da barbearia nova (autossuficiente, não usa app.js)
setup.html       # painel da JOGA: link, importar/exportar, conferir e aplicar
manifest.json · sw.js  # PWA (service worker network-first)
docker-compose.prod.yml · .github/workflows/deploy.yml · Dockerfile
```
