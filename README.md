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
- **Comissão**:
  - **Avulso**: 45% (config por barbeiro) sobre o valor do serviço executado.
  - **Assinante (POOL)**: 45% (`config.comissao_padrao`) da **arrecadação dos planos** no período,
    rateado pela **produção** (1 visita = 1 atendimento, mesmo com vários serviços). A **dona não
    recebe comissão** (`recebe_comissao=false`); a fração dos atendimentos dela fica com a casa.
  - **Fechar/pagar comissão** por barbeiro/quinzena → vira **despesa "Comissões" no caixa** e marca
    pago/pendente (tabela `comissoes_pagas`). Filtro por barbeiro + "fechar todos os pendentes".
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

## Variáveis de ambiente (`.env` / Portainer)
```
SECRET_KEY            # chave do Flask
DB_HOST/PORT/NAME/USER/PASSWORD
SEED_SENHA_INICIAL    # senha inicial dos usuários semeados (troca no 1º login)
SUPORTE_EMAIL         # acesso master JOGA (vazio = desativado)
SUPORTE_SENHA
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
login.html · trocar-senha.html
manifest.json · sw.js  # PWA (service worker network-first)
docker-compose.prod.yml · .github/workflows/deploy.yml · Dockerfile
```
