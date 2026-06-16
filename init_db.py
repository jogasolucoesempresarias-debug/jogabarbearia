"""Inicializa o banco joga_barbearia. Rode 1x: python -X utf8 init_db.py
Idempotente. Os DADOS de exemplo ficam em seed_barbearia.py (rode depois)."""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(
    host=os.getenv('DB_HOST', 'localhost'),
    port=os.getenv('DB_PORT', '5432'),
    dbname=os.getenv('DB_NAME', 'joga_barbearia'),
    user=os.getenv('DB_USER', 'postgres'),
    password=os.getenv('DB_PASSWORD', ''),
)
cur = conn.cursor()

# ── Profissionais (barbeiros) ─────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS profissionais (
        id           SERIAL PRIMARY KEY,
        nome         VARCHAR(120) NOT NULL,
        comissao_pct NUMERIC(5,2) DEFAULT 45,
        cor_agenda   VARCHAR(9) DEFAULT '#38bdf8',
        ativo        BOOLEAN DEFAULT true
    );
""")
# Dono-barbeiro não recebe comissão (o que atende é da casa)
cur.execute("ALTER TABLE profissionais ADD COLUMN IF NOT EXISTS recebe_comissao BOOLEAN DEFAULT true;")

# ── Usuários (login) — dono / caixa / barbeiro ────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id                   SERIAL PRIMARY KEY,
        nome                 VARCHAR(255) NOT NULL,
        email                VARCHAR(255) UNIQUE NOT NULL,
        password_hash        VARCHAR(255) NOT NULL,
        role                 VARCHAR(12) NOT NULL DEFAULT 'caixa'
                             CHECK (role IN ('dono', 'caixa', 'barbeiro')),
        profissional_id      INTEGER REFERENCES profissionais(id) ON DELETE SET NULL,
        ativo                BOOLEAN DEFAULT true,
        must_change_password BOOLEAN DEFAULT true,
        criado_em            TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_usuarios_email ON usuarios(email);")

# ── Clientes ──────────────────────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS clientes (
        id                  SERIAL PRIMARY KEY,
        nome                VARCHAR(255) NOT NULL,
        telefone            VARCHAR(30),
        tipo                VARCHAR(10) NOT NULL DEFAULT 'universal'
                            CHECK (tipo IN ('fixo', 'universal')),
        profissional_fixo_id INTEGER REFERENCES profissionais(id) ON DELETE SET NULL,
        observacoes         TEXT,
        ativo               BOOLEAN DEFAULT true,
        criado_em           TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_clientes_nome ON clientes(nome);")
# Autocadastro via QR: clientes vindos do QR entram como 'pendente' até a dona aprovar.
# status='aprovado' (default) faz o backfill dos registros existentes — nada quebra.
cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS status VARCHAR(10) NOT NULL DEFAULT 'aprovado';")
cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS origem VARCHAR(12) NOT NULL DEFAULT 'manual';")

# ── Serviços ──────────────────────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS servicos (
        id          SERIAL PRIMARY KEY,
        nome        VARCHAR(120) NOT NULL,
        preco       NUMERIC(10,2) NOT NULL,
        duracao_min INTEGER NOT NULL DEFAULT 30,
        ativo       BOOLEAN DEFAULT true
    );
""")

# ── Produtos (estoque pré-moldado, off no v1) ─────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS produtos (
        id      SERIAL PRIMARY KEY,
        nome    VARCHAR(120) NOT NULL,
        preco   NUMERIC(10,2) NOT NULL,
        estoque INTEGER,                 -- NULL = sem controle (v1)
        ativo   BOOLEAN DEFAULT true
    );
""")

# ── Planos de assinatura (configuráveis) ──────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS planos (
        id                       SERIAL PRIMARY KEY,
        nome                     VARCHAR(120) NOT NULL,
        valor_mensal             NUMERIC(10,2) NOT NULL DEFAULT 0,
        limite_uso               INTEGER,         -- NULL = ilimitado
        dias_inclusos            JSONB DEFAULT '[1,2,3]'::jsonb,   -- 0=dom..6=sab
        comissao_assinante_regra VARCHAR(10) DEFAULT 'tabela'
                                 CHECK (comissao_assinante_regra IN ('tabela', 'fixo', 'zero')),
        comissao_assinante_valor NUMERIC(10,2),
        ativo                    BOOLEAN DEFAULT true
    );
""")

# Serviços inclusos no plano (m2m)
cur.execute("""
    CREATE TABLE IF NOT EXISTS plano_servicos (
        plano_id   INTEGER NOT NULL REFERENCES planos(id) ON DELETE CASCADE,
        servico_id INTEGER NOT NULL REFERENCES servicos(id) ON DELETE CASCADE,
        PRIMARY KEY (plano_id, servico_id)
    );
""")

# ── Assinaturas (cliente + plano) ─────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS assinaturas (
        id             SERIAL PRIMARY KEY,
        cliente_id     INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
        plano_id       INTEGER NOT NULL REFERENCES planos(id) ON DELETE RESTRICT,
        dia_vencimento INTEGER NOT NULL DEFAULT 10 CHECK (dia_vencimento IN (10, 30)),
        status         VARCHAR(10) NOT NULL DEFAULT 'ativa'
                       CHECK (status IN ('ativa', 'pausada', 'cancelada')),
        data_inicio    DATE NOT NULL DEFAULT CURRENT_DATE,
        data_fim       DATE,
        criado_em      TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_assin_cliente ON assinaturas(cliente_id);")
# Data da próxima cobrança (recorrência por dia 10/30 a partir do mês seguinte à assinatura)
cur.execute("ALTER TABLE assinaturas ADD COLUMN IF NOT EXISTS proxima_cobranca DATE;")

# ── Agendamentos ──────────────────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS agendamentos (
        id              SERIAL PRIMARY KEY,
        profissional_id INTEGER NOT NULL REFERENCES profissionais(id) ON DELETE CASCADE,
        cliente_id      INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
        servico_id      INTEGER REFERENCES servicos(id) ON DELETE SET NULL,
        data            DATE NOT NULL,
        hora_inicio     TIME NOT NULL,
        duracao_slots   INTEGER NOT NULL DEFAULT 1,
        status          VARCHAR(12) NOT NULL DEFAULT 'agendado'
                        CHECK (status IN ('agendado', 'atendido', 'cancelado', 'falta')),
        origem          VARCHAR(8) DEFAULT 'agenda' CHECK (origem IN ('agenda', 'walkin')),
        observacao      TEXT,
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em       TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_agend_data ON agendamentos(data, profissional_id);")
# Múltiplos serviços por agendamento (lista de ids); servico_id segue como "principal" p/ display
cur.execute("ALTER TABLE agendamentos ADD COLUMN IF NOT EXISTS servicos_ids JSONB DEFAULT '[]'::jsonb;")

# ── Bloqueios de agenda (folga, almoço, férias) ───────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS bloqueios (
        id              SERIAL PRIMARY KEY,
        profissional_id INTEGER NOT NULL REFERENCES profissionais(id) ON DELETE CASCADE,
        data            DATE NOT NULL,
        hora_inicio     TIME NOT NULL,
        hora_fim        TIME NOT NULL,
        motivo          VARCHAR(120)
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_bloq_data ON bloqueios(data, profissional_id);")

# ── Comandas (o pivô) ─────────────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS comandas (
        id              SERIAL PRIMARY KEY,
        cliente_id      INTEGER REFERENCES clientes(id) ON DELETE SET NULL,
        profissional_id INTEGER REFERENCES profissionais(id) ON DELETE SET NULL,
        agendamento_id  INTEGER REFERENCES agendamentos(id) ON DELETE SET NULL,
        status          VARCHAR(10) NOT NULL DEFAULT 'aberta'
                        CHECK (status IN ('aberta', 'fechada', 'cancelada')),
        forma_pagamento VARCHAR(20),
        valor_total     NUMERIC(10,2) DEFAULT 0,
        aberta_em       TIMESTAMP DEFAULT NOW(),
        fechada_em      TIMESTAMP,
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_comanda_status ON comandas(status);")

# ── Itens da comanda (serviço/produto) ────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS comanda_itens (
        id              SERIAL PRIMARY KEY,
        comanda_id      INTEGER NOT NULL REFERENCES comandas(id) ON DELETE CASCADE,
        tipo            VARCHAR(8) NOT NULL CHECK (tipo IN ('servico', 'produto')),
        ref_id          INTEGER,                  -- servico_id ou produto_id
        descricao       VARCHAR(160) NOT NULL,
        profissional_id INTEGER REFERENCES profissionais(id) ON DELETE SET NULL,  -- executor (comissão)
        preco_unit      NUMERIC(10,2) NOT NULL DEFAULT 0,
        qtd             INTEGER NOT NULL DEFAULT 1,
        subtotal        NUMERIC(10,2) NOT NULL DEFAULT 0,
        preco_tabela    NUMERIC(10,2),            -- preço cheio do serviço (base de comissão se coberto)
        coberto_plano   BOOLEAN DEFAULT false,
        assinatura_id   INTEGER REFERENCES assinaturas(id) ON DELETE SET NULL
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_item_comanda ON comanda_itens(comanda_id);")

# ── Movimentos financeiros (caixa) ────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS movimentos (
        id              SERIAL PRIMARY KEY,
        tipo            VARCHAR(8) NOT NULL CHECK (tipo IN ('receita', 'despesa')),
        origem          VARCHAR(12) NOT NULL DEFAULT 'manual'
                        CHECK (origem IN ('comanda', 'assinatura', 'manual')),
        ref_id          INTEGER,
        descricao       VARCHAR(200) NOT NULL,
        valor           NUMERIC(10,2) NOT NULL,
        forma_pagamento VARCHAR(20),
        data            DATE NOT NULL DEFAULT CURRENT_DATE,
        vencimento      DATE,
        status          VARCHAR(10) NOT NULL DEFAULT 'pago'
                        CHECK (status IN ('previsto', 'pago')),
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em       TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_mov_data ON movimentos(data);")
cur.execute("CREATE INDEX IF NOT EXISTS ix_mov_status ON movimentos(status);")
# Despesas: categoria + vínculo com despesa fixa recorrente (aditivo)
cur.execute("ALTER TABLE movimentos ADD COLUMN IF NOT EXISTS categoria VARCHAR(40);")
cur.execute("ALTER TABLE movimentos ADD COLUMN IF NOT EXISTS despesa_fixa_id INTEGER;")

# ── Despesas fixas (recorrentes: aluguel, impostos, internet...) ──────
cur.execute("""
    CREATE TABLE IF NOT EXISTS despesas_fixas (
        id        SERIAL PRIMARY KEY,
        descricao VARCHAR(160) NOT NULL,
        categoria VARCHAR(40),
        valor     NUMERIC(10,2) NOT NULL,
        dia       INTEGER DEFAULT 5 CHECK (dia BETWEEN 1 AND 28),
        ativo     BOOLEAN DEFAULT true,
        criado_em TIMESTAMP DEFAULT NOW()
    );
""")

# ── Configurações (linha única) + white-label ─────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS configuracoes (
        id                INTEGER PRIMARY KEY DEFAULT 1,
        slot_min          INTEGER DEFAULT 30,
        comissao_padrao   NUMERIC(5,2) DEFAULT 45,
        formas_pagamento  JSONB DEFAULT '["Dinheiro","Pix","Cartão"]'::jsonb,
        horarios          JSONB DEFAULT '{}'::jsonb,
        taxa_cartao_pct   NUMERIC(5,2),
        cancelamento_taxa NUMERIC(10,2),
        marca_nome        VARCHAR(120) DEFAULT 'JOGA Barbearia',
        marca_logo_url    VARCHAR(255),
        marca_cor         VARCHAR(9) DEFAULT '#38bdf8',
        CONSTRAINT config_single_row CHECK (id = 1)
    );
""")
cur.execute("INSERT INTO configuracoes (id) VALUES (1) ON CONFLICT (id) DO NOTHING;")
# Categorias de despesa (configuráveis, incluindo Impostos)
cur.execute("""ALTER TABLE configuracoes ADD COLUMN IF NOT EXISTS categorias_despesa JSONB
    DEFAULT '["Impostos","Aluguel","Insumos/Produtos","Energia/Água","Salários","Comissões","Marketing","Manutenção","Outras"]'::jsonb;""")

# ── Comissões pagas (fechamento por barbeiro/período → despesa no caixa) ──
cur.execute("""
    CREATE TABLE IF NOT EXISTS comissoes_pagas (
        id              SERIAL PRIMARY KEY,
        profissional_id INTEGER NOT NULL REFERENCES profissionais(id) ON DELETE CASCADE,
        periodo_de      DATE NOT NULL,
        periodo_ate     DATE NOT NULL,
        valor           NUMERIC(10,2) NOT NULL,
        data_pagamento  DATE NOT NULL DEFAULT CURRENT_DATE,
        movimento_id    INTEGER REFERENCES movimentos(id) ON DELETE SET NULL,
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em       TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_comissoes_prof ON comissoes_pagas(profissional_id, periodo_de, periodo_ate);")

conn.commit()
cur.close()
conn.close()
print("[OK] Schema pronto. Rode: python -X utf8 seed_barbearia.py")
