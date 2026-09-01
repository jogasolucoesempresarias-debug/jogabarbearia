# JOGA Barbearia

SaaS de gestão para barbearia — **mobile-first + PWA**. Agenda interna, comanda (serviços + produtos),
planos de assinatura, comissão dos barbeiros, caixa, despesas/impostos e DRE.

Construído sobre a espinha do **Gestão JOGA** (auth, financeiro, deploy), como produto vertical da
JOGA Soluções Empresariais. **Instância própria por cliente** (banco/subdomínio próprios).

## Stack
Flask + Waitress · PostgreSQL · HTML/CSS/JS puro (mobile-first) + PWA · Docker Swarm/Traefik/GHCR.
Fuso horário **America/Sao_Paulo** (app via `TZ` e conexão do banco via `options`).

## Instâncias em produção

**Uma imagem só** (`ghcr.io/jogasolucoesempresarias-debug/jogabarbearia:latest`) serve as três. O que
muda entre elas é **variável de ambiente e banco** — não há branch nem repositório separado.

| Domínio | O que é | Banco | Stack (Portainer) | Serviço (Swarm) | Modo |
|---|---|---|---|---|---|
| `barbearia.jogasolucoes.com.br` | Cliente real (Regiane) | `joga_barbearia` | `barbearia` | `barbearia_barbearia-app` | — |
| `coletabarbearia.jogasolucoes.com.br` | Hub de fichas dos prospects | `joga_coleta` | `coletabarbearia` | `coletabarbearia_coleta-app` | `MODO_COLETA=1` |
| `demobarbearia.jogasolucoes.com.br` | Demonstração pública | `barbearia_demo` | `barbearia_demo` | `barbearia_demo_app` | `MODO_DEMO=1` |

Cliente novo = **subdomínio novo** de `jogasolucoes.com.br` (não se compra domínio), banco novo e
stack nova. Com um registro DNS wildcard (`*.jogasolucoes.com.br → IP do VPS`) não é preciso mexer no
DNS a cada cliente; o certificado sai por domínio, via desafio HTTP do Let's Encrypt.

> ⚠️ **O nome do router/service do Traefik é único no SERVIDOR INTEIRO, não por stack.** Nome repetido
> faz o router ser **descartado em silêncio**: não há rota, não há pedido de certificado, e o
> navegador recebe `TRAEFIK DEFAULT CERT` — **sem uma linha de erro no log do Traefik**. Foi o que
> aconteceu ao usar `demo`, que já existia no host. Antes de subir stack nova, confira:
> `docker service ls --format "{{.Name}}"` e escolha um nome que não exista.
>
> ⚠️ Cada instância precisa de **`SECRET_KEY` própria**. Repetida, o cookie de sessão de uma vale na
> outra.

## Acessos (login e senha)

**Nenhuma senha fica neste repositório.** Os `docker-compose.*.yml` usam `${VARIÁVEL}`; os valores
reais vivem só no Portainer. Senha de banco e senha do master comitadas ficam no histórico do git
para sempre, mesmo depois de trocadas.

| Instância | Como se entra |
|---|---|
| **Cliente (Regiane)** | Usuários no banco (`dono`, `caixa`, `barbeiro`), criados pelo seed ou pelo *Aplicar* da ficha. Senha inicial = `SEED_SENHA_INICIAL`, **trocada no 1º acesso** (`must_change_password`). Mais o master da JOGA por env. |
| **Hub de coleta** | **Só** o master da JOGA (`SUPORTE_EMAIL`/`SUPORTE_SENHA`). Não existe usuário no banco: se essas envs estiverem vazias ou erradas, **ninguém entra, nem você**. O prospect não faz login — a ficha abre por token no link. |
| **Demonstração** | Dois usuários no banco com **senha publicada na própria tela de login** (`SENHA_DEMO`, padrão `demo`): `ze@barbearia.local` (dono) e `rafael@barbearia.local` (barbeiro). Sem troca de senha obrigatória — o prospect não pode ser barrado. **Não configure `SUPORTE_*` aqui**: é a instância mais exposta que existe e o acesso master não tem por que viver nela. |

O **master (`SUPORTE_EMAIL`/`SUPORTE_SENHA`)** não fica no banco: é comparado direto contra a env, com
`hmac.compare_digest`, e entra com role `dono` e `user_id` nulo (por isso `criado_por` fica NULL no que
ele cria). Só existe se as duas envs estiverem preenchidas. Serve para configurar e dar suporte sem
criar usuário na barbearia do cliente.

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
- **Caixa**: fechamento do dia por forma de pagamento + a receber/lançamentos, com **bruto ×
  taxas × líquido** quando houve cartão no dia.
- **Taxas de maquininha**: cada forma de pagamento tem seu **% próprio** (tabela
  `formas_pagamento`, editável em *Configurações › Pagamentos*). A cada recebimento o sistema
  lança a taxa como **despesa** na categoria "Taxas de cartão", amarrada ao movimento de receita
  (`origem='taxa'`, `ref_id` = id da receita) — some junto quando a receita é desfeita.
  - **A receita continua BRUTA de propósito.** A taxa é custo, não desconto no faturamento. Se
    fosse abatida da receita, a **comissão do barbeiro** (que sai de `comanda_itens.subtotal`)
    mudaria conforme a forma de pagamento escolhida pelo *cliente* — ninguém decidiu isso.
  - Nasce da tabela em **quatro** pontos (motor único `registrar_taxa`): fechar comanda, criar
    assinatura, receita manual e **`mov_pagar`** (onde a mensalidade prevista vira paga e ganha
    forma — o mais fácil de esquecer). Lançada na **data da venda**, não no D+30 do recebimento.
  - Tela `/taxas`: bruto, taxa e líquido do período por modalidade, com o **% efetivo calculado
    do realizado** (se a taxa mudou no meio do período, vale o que foi lançado na época).
  - `configuracoes.formas_pagamento` (JSONB) virou **vestigial**: a tabela é a fonte da verdade.
    O `/api/config` segue devolvendo a lista de **nomes** porque seis telas consomem esse formato.
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
  sem taxa, horário por dia. O backend valida **sobreposição de intervalo** (não só o mesmo minuto
  de início), bloqueio e horário de funcionamento — motor único `slot_livre`, usado também pela
  porta pública.
- **Agendamento online** (`/agendamento`, público, opt-in em *Configurações › Agendamento*): o cliente
  marca sozinho pelo link — **sem app, sem conta**, nome e telefone bastam. Escolhe serviço(s) →
  barbeiro (ou "tanto faz") → dia → horário. Depois volta na mesma página, entra com **nome +
  telefone** e vê/cancela os próprios horários.
  - **Nasce desligado** (`agendamento_online=false`): instância existente não ganha porta pública
    sem alguém decidir. `/agendamento` responde 404 enquanto estiver desligado.
  - **Confirmar manual** (padrão): o pedido entra como `status='pendente'` e a barbearia aceita na
    Agenda (slot tracejado, ⏳). Desligado, cai direto como `agendado`.
  - A **disponibilidade responde só livre/ocupado** — nunca nome de cliente. O `/api/agenda`
    (que devolve nome de todo mundo do dia) é interno e **não pode ser reaproveitado ali**.
  - Travas: rate limit por IP (**a cota só é gasta quando o agendamento nasce** — cobrar tentativa
    com erro trancaria quem só errou o próprio telefone), honeypot, **1 horário futuro em aberto
    por telefone**, antecedência mínima e janela máxima configuráveis, `aceita_online` por barbeiro.
  - Cliente novo entra com `origem='online'` e `status='aprovado'` — o agendamento em si é o portão.
- **Mensagens de WhatsApp — envio MANUAL**: nada é disparado. O sistema **monta o texto e devolve
  um link `wa.me`**; a barbearia toca, o WhatsApp **dela** abre com a mensagem pronta e ela envia.
  Duas filas, num cartão no topo da Agenda:
  - **aceite** — ela aprovou um pedido online e o cliente está esperando resposta;
  - **lembrete D-1** — véspera do atendimento, para todo mundo (online ou do balcão).
  - Por que manual e por `wa.me`: a mensagem sai do **número real da barbearia** (o cliente
    reconhece e responde ali), não existe instância de WhatsApp por cliente pra conectar e pagar,
    e não há risco de bloqueio por disparo em massa.
  - **A fila é uma consulta** (`confirmacao_enviada_em` / `lembrete_enviado_em` nulas) — por isso
    não há cron nem agendador no servidor.
  - O sistema **não sabe** se ela apertou enviar (o `wa.me` não devolve nada): marca no clique e
    oferece **desfazer**. Nunca exibimos "entregue".
  - ⚠️ `normalizar_telefone_br` **remove o 9º dígito** (canônico da UazAPI) e **não serve aqui** —
    o `wa.me` quer o número cheio. Helper próprio: `wa_numero`.
  - ⚠️ **Sem emoji no texto da mensagem.** O link sai em UTF-8 correto, mas o Windows repassa a
    URL ao WhatsApp Desktop convertendo para a codepage ANSI: acento sobrevive (existe em
    Latin-1), emoji vira `?` **na tela do cliente da barbearia**. `_smoke_agendar.py` trava
    qualquer caractere acima de `U+00FF` no texto — não é firula, é o que impede a regressão.
  - O canal **UazAPI (`alerta_whatsapp`) não é usado nisso**: aquilo é o aviso interno da JOGA
    quando um prospect envia a ficha.
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
  `0 4 * * * docker exec $(docker ps -q -f name=barbearia_demo) python -X utf8 seed_demo.py`.
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
SECRET_KEY            # chave do Flask — PRÓPRIA por instância (repetida, a sessão vaza entre elas)
DB_HOST/PORT/NAME/USER/PASSWORD
SEED_SENHA_INICIAL    # senha inicial dos usuários semeados (troca no 1º login)
SUPORTE_EMAIL         # acesso master JOGA (vazio = desativado). NÃO usar na demo
SUPORTE_SENHA
MODO_COLETA           # 1 = hub de coleta (não é barbearia; guarda N fichas). Vazio = instância normal
MODO_DEMO             # 1 = demonstração: senha na tela de login, /setup fechado, libera o seed_demo
SENHA_DEMO            # senha dos logins da demo (padrão: demo)
DEMO_DIAS             # dias de histórico gerados pelo seed_demo (padrão: 180)
ALERTAS_ATIVO         # 1 = dispara o WhatsApp de verdade. Vazio = só loga (dev/testes)
UAZAPI_URL            # https://free.uazapi.com
UAZAPI_TOKEN
ALERTA_WHATSAPP       # quem recebe o aviso, separado por vírgula
```

Combinações válidas: nenhum modo (barbearia de cliente) · `MODO_COLETA=1` (hub) · `MODO_DEMO=1`
(demonstração). **Nunca os dois juntos** — o hub esconde a navegação da barbearia e a demo precisa
dela.

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

Para rodar os outros dois modos localmente, basta banco próprio + a env do modo:
```powershell
# hub de coleta — entra pelo master (SUPORTE_EMAIL/SUPORTE_SENHA do .env)
$env:DB_NAME="joga_coleta_local"; $env:MODO_COLETA="1"; $env:PORT="5005"
.\.venv\Scripts\python.exe -X utf8 init_db.py; .\.venv\Scripts\python.exe -X utf8 server.py

# demonstração — logins ze@ / rafael@ com a senha `demo`
$env:DB_NAME="joga_demo_local"; $env:MODO_DEMO="1"; $env:PORT="5006"
.\.venv\Scripts\python.exe -X utf8 init_db.py
.\.venv\Scripts\python.exe -X utf8 seed_demo.py       # ~40s: gera 180 dias de histórico
.\.venv\Scripts\python.exe -X utf8 server.py
```

### Smoke tests (cada um limpa o resíduo no fim)
- `_smoke.py` — fluxo operacional (cliente, agenda, comanda, caixa, assinatura, comissão)
- `_smoke_pool.py` — rateio do bolo por visita
- `_smoke_gestao.py` — timezone, dona fora do rateio, fechar comissão→despesa, DRE
- `_smoke_despesas.py` — despesas, categorias, fixas (gerar do mês)
- `_smoke_assinatura.py` — 1ª no caixa hoje + próxima no dia 10/30 do mês seguinte
- `_smoke_uso.py` — registrar visita de assinante, Uso dos Planos e DRE aprimorado (test_client)
- `_smoke_regras.py` — as 4 regras de comissão do assinante + o ajuste manual no fechamento
- `_smoke_onboarding.py` — ficha → aplicar → barbearia nascida, **num banco descartável próprio**
  (cria/derruba `joga_barbearia_smoke` e sobe um servidor só dele; não toca no banco de dev)
- `_smoke_hub.py` — hub `MODO_COLETA=1`: N fichas convivendo, token de uma não abre a outra,
  aplicar bloqueado (banco descartável próprio)
- `_smoke_alerta.py` — alerta de WhatsApp: sobe uma **UazAPI de mentira** local e confere número
  normalizado, header do token, texto e os 2 destinatários — sem gastar mensagem nem usar internet
- `_smoke_taxas.py` — taxa da maquininha: os **4 pontos onde ela nasce** e os **3 de desfazer**,
  o líquido no caixa do dia, o relatório do período e a trava de que **a receita continua bruta**
- `_smoke_agendar.py` — agendamento online ponta a ponta em **banco descartável próprio**: a trava
  de sobreposição/bloqueio/horário, a porta pública (404 desligada), **disponibilidade sem vazar
  nome de cliente**, agendar/cancelar, "meus horários" só devolvendo os da pessoa, as travas de
  abuso, a fila de mensagens e o **`wa.me` mantendo o 9º dígito**
- `_smoke_demo.py` — a trava do `seed_demo.py`, o reset sem duplicar, **a ficha de coleta
  sobrevivendo ao reset** e as telas que o prospect vê (~50s: roda o seed completo de propósito,
  porque "dormente" e o gráfico de 6 meses só existem na janela real de 180 dias)

> Os smokes que sobem servidor escolhem **porta livre pelo SO**. Porta fixa fazia o teste conversar
> com um servidor de dev já rodando e validar a instância errada em silêncio.
- `_teste_completo.py` — bateria ponta a ponta

## Deploy

`git push main` → GitHub Actions builda e publica no GHCR com **duas tags**: `:latest` e
`:sha-<commit>`. Migrações são aditivas (`IF NOT EXISTS`) e **nunca destroem dado**.

**Confira que o build terminou antes de atualizar o serviço.** O Swarm usa `stop-first`: se a imagem
não existir, ele **derruba o container antigo**, falha ao subir o novo e o serviço fica em **0/1**,
fora do ar. Recuperação: `docker service update --rollback <serviço>`.

> ⚠️ A tag usa o **sha COMPLETO de 40 caracteres** (`sha-${{ github.sha }}` no workflow), não o
> curto de 7 que o `git log` mostra. `git rev-parse HEAD` dá o certo — com o curto, o `pull` falha
> com "manifest unknown".

```bash
# 1. confira a tag no GHCR (ou espere o check verde no Actions)
SHA=$(git rev-parse HEAD)      # ou cole o sha completo à mão
sudo docker pull ghcr.io/jogasolucoesempresarias-debug/jogabarbearia:sha-$SHA

# 2. atualize a instância desejada — use o NOME COMPLETO do serviço
sudo docker service update --image ghcr.io/jogasolucoesempresarias-debug/jogabarbearia:sha-$SHA \
     --force barbearia_barbearia-app
sudo docker exec $(sudo docker ps -q -f name=barbearia_barbearia) python -X utf8 init_db.py
```

> ⚠️ `docker ps -f name=barbearia` casa com **a produção da Regiane e a demo** ao mesmo tempo. Use
> sempre o nome completo (`barbearia_barbearia`, `barbearia_demo`) e confira antes com
> `docker ps --format "{{.Names}}" | grep barbearia`.

> **Nesta leva o `init_db.py` é obrigatório depois do update.** Ele cria `formas_pagamento`
> (semeando com o que a instância já usava, taxa 0 — ninguém perde nem ganha nada), altera os
> CHECK de `movimentos.origem` e `agendamentos.origem/status`, e acrescenta as colunas do
> agendamento online. Tudo aditivo. O **agendamento online nasce desligado**: ligue em
> *Configurações › Agendamento* quando o cliente quiser, e preencha as taxas da maquininha dele
> em *Configurações › Pagamentos* (entram como 0%, ou seja, sem efeito, até serem preenchidas).

**Instância nova (cliente, hub ou demo)** — a ordem importa:
```bash
# 1. banco (o container do Postgres é o `postgres_postgres`, não os outros que casam com "postgres")
sudo docker exec -it $(sudo docker ps -q -f name=postgres_postgres) psql -U admin -d postgres -c "CREATE DATABASE <banco>"
# 2. stack no Portainer (docker-compose.prod / .coleta / .demo), com SECRET_KEY própria e router Traefik de nome inédito
# 3. schema
sudo docker exec $(sudo docker ps -q -f name=<stack>) python -X utf8 init_db.py
# 4. dados: seed_barbearia.py (cliente) OU seed_demo.py (demo) OU nada (hub — ele nasce vazio de propósito)
```

Reinstalação limpa (teste): scale 0 → drop/create DB → scale 1 → `init_db` + seed.

### Manutenção da demo
```bash
# reset manual (o mesmo script que popula) — apaga e refaz com as datas atualizadas
sudo docker exec $(sudo docker ps -q -f name=barbearia_demo) python -X utf8 seed_demo.py
```

O reset diário vai no **crontab do root** (`sudo crontab -e`), não no do usuário: o usuário comum não
tem acesso ao `docker.sock` nem escreve em `/var/log`, e no cron não há como usar `sudo`. Caminho
absoluto porque o cron roda com `PATH` mínimo:

```
0 4 * * * /usr/local/bin/reset-demo-barbearia.sh >> /var/log/barbearia-demo.log 2>&1
```

```sh
# /usr/local/bin/reset-demo-barbearia.sh  (chmod +x)
#!/bin/sh
CID=$(/usr/bin/docker ps -q -f name=barbearia_demo)
if [ -z "$CID" ]; then
  echo "$(date -Is) [ERRO] container barbearia_demo nao encontrado"   # container reiniciando
  exit 1
fi
echo "$(date -Is) reiniciando a demo no container $CID"
/usr/bin/docker exec "$CID" python -X utf8 seed_demo.py
```

## Estrutura
```
server.py            # backend: auth+RBAC, agenda, comanda, assinaturas, despesas, caixa, comissões, DRE
init_db.py           # schema (idempotente, migrações aditivas + _migracoes p/ migração de DADO one-shot)
seed_barbearia.py    # dados pré-configurados (serviços, produtos, 2 barbeiros, horários, 3 planos)
seed_demo.py         # popula E reseta a demo (datas relativas a hoje; exige MODO_DEMO)
static/app.css|js    # shell mobile-first (bottom-nav, máscara telefone, helpers)
agenda · comanda · clientes · assinaturas · uso(Uso dos Planos) · caixa · despesas · relatorios(Comissões) · taxas · dre · config · barbeiro .html
login.html · trocar-senha.html · cadastro.html(QR público) · agendar.html(agendamento online público, servido em `/agendamento`)
coleta.html      # ficha pública da barbearia nova (autossuficiente, não usa app.js)
setup.html       # painel da JOGA: link, importar/exportar, conferir e aplicar (lista de fichas no hub)
manifest.json · sw.js  # PWA (service worker network-first)
docker-compose.prod.yml    # cliente   → barbearia.jogasolucoes.com.br
docker-compose.coleta.yml  # hub       → coletabarbearia.jogasolucoes.com.br  (MODO_COLETA=1)
docker-compose.demo.yml    # demo      → demobarbearia.jogasolucoes.com.br    (MODO_DEMO=1)
.github/workflows/deploy.yml · Dockerfile
```
