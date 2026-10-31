-- SYNTHETIC demo seed. Fictional people and institutions on .example domains.
-- Product names are Azentio's public product lines; every fact below is SAMPLE CONTENT, not Azentio policy.

-- ───────────── Users ─────────────
INSERT INTO users (name, email, role, region) VALUES
  ('Demo Admin',      'admin@azentio-demo.example',        'admin',     NULL),
  ('SDR Reviewer',    'reviewer@azentio-demo.example',     'reviewer',  NULL),
  ('Priya Nair',      'priya.nair@azentio-demo.example',   'sdr',       NULL),
  ('Aisha Rahman',    'aisha.rahman@azentio-demo.example', 'sales_rep', 'GCC'),
  ('Rohan Mehta',     'rohan.mehta@azentio-demo.example',  'sales_rep', 'India'),
  ('Kwame Mensah',    'kwame.mensah@azentio-demo.example', 'sales_rep', 'Africa'),
  ('Daniel Ortiz',    'daniel.ortiz@azentio-demo.example', 'sales_rep', 'International');

-- ───────────── Products and approved facts ─────────────
INSERT INTO products (slug, name, category, summary, keywords) VALUES
  ('kastle', 'Kastle (Lending)', 'lending', 'SAMPLE: lending platform for origination, servicing and collections.',
   ARRAY['lending','loan','origination','los','lms','financing','credit','collections']),
  ('amlock', 'AMLOCK (AML & Compliance)', 'compliance', 'SAMPLE: AML and compliance suite.',
   ARRAY['aml','anti-money laundering','compliance','screening','sanctions','transaction monitoring','false positives','kyc']),
  ('imal', 'iMAL (Islamic Core Banking)', 'core_banking', 'SAMPLE: Shariah-compliant core banking platform.',
   ARRAY['core banking','islamic banking','shariah','murabaha','ijarah','islamic window']);

INSERT INTO product_facts (product_id, fact_type, statement, approved_by, approved_at, source_ref)
SELECT p.id, f.fact_type, f.statement, (SELECT id FROM users WHERE role = 'admin'), now(), 'SAMPLE CONTENT'
FROM (VALUES
  ('kastle', 'capability', 'Kastle is a lending platform that covers loan origination, servicing and collections in one suite.'),
  ('kastle', 'capability', 'Kastle supports both conventional and Islamic financing products.'),
  ('amlock', 'capability', 'AMLOCK is an AML and compliance suite covering transaction monitoring, customer screening and case management.'),
  ('amlock', 'capability', 'AMLOCK helps compliance teams prioritise alerts so analysts spend time on the cases that matter.'),
  ('imal',   'capability', 'iMAL is a Shariah-compliant core banking platform built for Islamic banks and Islamic windows.')
) AS f(slug, fact_type, statement)
JOIN products p ON p.slug = f.slug;

-- ───────────── ICP profiles and criteria (rule formats documented in backend/app/engine/fixture.py) ─────────────
INSERT INTO icp_profiles (product_id, name, version, high_threshold, medium_threshold)
SELECT id, name || ' ICP', 1, 75, 45 FROM products;

INSERT INTO icp_criteria (icp_profile_id, dimension, weight, rule, description)
SELECT ip.id, c.dimension, c.weight, c.rule::jsonb, c.description
FROM (VALUES
  ('kastle', 'geography',         25, '{"countries":["SA","AE","QA","KW","BH","OM","IN","KE","NG","EG","ZA","MY","ID"]}', 'GCC, India, Africa, SEA'),
  ('kastle', 'institution_type',  25, '{"in":["bank","islamic_bank","nbfc","microfinance"]}', 'Lenders'),
  ('kastle', 'role',              20, '{"seniority_in":["C-level","VP","Head","Director"]}', 'Decision makers'),
  ('kastle', 'org_size',          15, '{"min_employees":200}', '200+ employees'),
  ('kastle', 'product_relevance', 15, '{"title_keywords":["lending","loan","credit","retail","sme","digital","transformation","operations","chief executive"],"departments":["lending","credit","operations","transformation","executive"]}', 'Role or enquiry relates to lending'),
  ('amlock', 'geography',         25, '{"countries":["SA","AE","QA","KW","BH","OM","IN","KE","NG","EG","ZA"]}', 'GCC, India, Africa'),
  ('amlock', 'institution_type',  25, '{"in":["bank","islamic_bank","exchange_house","nbfc"]}', 'Regulated FIs'),
  ('amlock', 'role',              20, '{"seniority_in":["C-level","VP","Head","Director"]}', 'Decision makers'),
  ('amlock', 'org_size',          15, '{"min_employees":200}', '200+ employees'),
  ('amlock', 'product_relevance', 15, '{"title_keywords":["compliance","risk","aml","financial crime","mlro","chief executive"],"departments":["compliance","risk","executive"]}', 'Role or enquiry relates to AML/compliance'),
  ('imal',   'geography',         25, '{"countries":["SA","AE","QA","KW","BH","OM","KE","NG","EG","MY","ID"]}', 'GCC, Africa, SEA'),
  ('imal',   'institution_type',  25, '{"in":["islamic_bank","bank"]}', 'Islamic banks and windows'),
  ('imal',   'role',              20, '{"seniority_in":["C-level","VP","Head","Director"]}', 'Decision makers'),
  ('imal',   'org_size',          15, '{"min_employees":300}', '300+ employees'),
  ('imal',   'product_relevance', 15, '{"title_keywords":["technology","information officer","cio","cto","transformation","operations","digital","core banking","chief executive"],"departments":["it","technology","transformation","operations","executive"]}', 'Role or enquiry relates to core banking')
) AS c(slug, dimension, weight, rule, description)
JOIN products p ON p.slug = c.slug
JOIN icp_profiles ip ON ip.product_id = p.id;

-- ───────────── Existing CRM accounts and an open opportunity (collision-shield fixtures) ─────────────
INSERT INTO accounts (name, domain, industry, institution_type, country, region, employee_count, is_customer, owner_id) VALUES
  ('Oasis Bank',           'oasisbank.example',    'Banking',            'islamic_bank', 'KW', 'GCC',    1900, true,  (SELECT id FROM users WHERE email = 'aisha.rahman@azentio-demo.example')),
  ('Savanna Trust Bank',   'savannatrust.example', 'Banking',            'bank',         'KE', 'Africa', 2100, true,  (SELECT id FROM users WHERE email = 'kwame.mensah@azentio-demo.example')),
  ('Indus Retail Finance', 'indusfin.example',     'Financial Services', 'nbfc',         'IN', 'India',  6000, false, (SELECT id FROM users WHERE email = 'rohan.mehta@azentio-demo.example'));

INSERT INTO opportunities (external_ref, account_id, product_id, owner_id, name, stage, status)
SELECT 'crm-opp-001', a.id, p.id, a.owner_id, 'Oasis Bank - AMLOCK upgrade', 'discovery', 'open'
FROM accounts a, products p WHERE a.domain = 'oasisbank.example' AND p.slug = 'amlock';

-- ───────────── Agents (one per engine role; versions hold the prompt/config actually used) ─────────────
INSERT INTO agents (key, name, role, description, provider, model) VALUES
  ('enricher',        'Enrichment Agent',  'enrichment',    'Looks up person and company (mock Apollo fixture).', 'fixture', 'mock_apollo'),
  ('icp_scorer',      'ICP Scorer',        'icp',           'Scores leads against the product ICP profile.',     'rules',   'icp-rules-v1'),
  ('outreach_writer', 'Outreach Writer',   'drafting',      'Drafts intro and follow-up emails from approved product facts only.', 'template', 'template-v1'),
  ('qualifier',       'Reply Qualifier',   'qualification', 'Classifies reply intent and extracts BANT evidence.', 'heuristic', 'heuristic-v1');

INSERT INTO agent_versions (agent_id, version, system_prompt, config, change_note)
SELECT id, 1, 'fixture implementation (replace with model prompt when a provider is connected)', '{}'::jsonb, 'initial skeleton'
FROM agents;
UPDATE agents a SET current_version_id = v.id FROM agent_versions v WHERE v.agent_id = a.id;

-- ───────────── Versioned policies ─────────────
INSERT INTO policies (kind, version, active, content) VALUES
  ('bant', 1, true, '{
     "rule": "REP_REVIEW when Need confirmed AND Timeline confirmed within 12 months AND (Budget OR Authority) confirmed. Inferred signals score but never qualify alone.",
     "points": {"confirmed": 25, "inferred": 12, "missing": 0, "conflict": 0},
     "minIntentConfidence": 0.6,
     "maxGapFollowups": 1,
     "gapOrder": ["need", "timeline", "authority", "budget"]
   }'),
  ('guardrail', 1, true, '{
     "approvalMode": "always",
     "bannedTerms": ["price","pricing","discount","cost","quote","guarantee","guaranteed","sla","commitment","contract","%","roi"],
     "dailySendCap": 20,
     "perContactDailyCap": 2,
     "personalEmailDomains": ["gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com"]
   }'),
  ('consent', 1, true, '{"consentRequiredCountries": ["DE","FR","NL","IE","ES","IT","BE","SE"]}'),
  ('routing', 1, true, '{"regions": {"GCC": ["SA","AE","QA","KW","BH","OM"], "India": ["IN"], "Africa": ["KE","NG","EG","ZA","GH","TZ"], "SEA": ["MY","ID","SG","PH"], "EU": ["DE","FR","NL","IE","ES","IT","BE","SE"]}}');

INSERT INTO demo_clock (id, offset_hrs) VALUES (1, 0);
