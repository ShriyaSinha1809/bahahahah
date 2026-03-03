-- =============================================================
-- Layer10 — Seed test data (Enron-style)
-- =============================================================

-- Entities
INSERT INTO entities (id, canonical_name, entity_type, aliases, properties) VALUES
  ('a0000000-0000-0000-0000-000000000001', 'Jeffrey Skilling', 'Person',
   ARRAY['Jeff Skilling', 'J. Skilling'], '{"title":"CEO","company":"Enron"}'),
  ('a0000000-0000-0000-0000-000000000002', 'Kenneth Lay', 'Person',
   ARRAY['Ken Lay', 'K. Lay'], '{"title":"Chairman","company":"Enron"}'),
  ('a0000000-0000-0000-0000-000000000003', 'Andrew Fastow', 'Person',
   ARRAY['Andy Fastow'], '{"title":"CFO","company":"Enron"}'),
  ('a0000000-0000-0000-0000-000000000004', 'Enron Corporation', 'Organization',
   ARRAY['Enron', 'ENE'], '{"ticker":"ENE","industry":"Energy"}'),
  ('a0000000-0000-0000-0000-000000000005', 'LJM Cayman', 'Organization',
   ARRAY['LJM', 'LJM2'], '{"type":"SPE","purpose":"off-balance-sheet"}'),
  ('a0000000-0000-0000-0000-000000000006', 'Project Raptors', 'Project',
   ARRAY['Raptors', 'Raptor SPEs'], '{"status":"active","risk":"high"}'),
  ('a0000000-0000-0000-0000-000000000007', 'Q3 2001 Earnings Call', 'Meeting',
   ARRAY['Q3 Earnings', 'Oct 2001 call'], '{"date":"2001-10-16","format":"conference_call"}'),
  ('a0000000-0000-0000-0000-000000000008', 'Arthur Andersen', 'Organization',
   ARRAY['Andersen', 'AA'], '{"type":"audit_firm"}')
ON CONFLICT (id) DO NOTHING;

-- Raw emails (minimal, for evidence pointers)
INSERT INTO raw_emails (message_id, sender, recipients, subject, body, date, body_hash, dedup_key) VALUES
  ('msg-001@enron.com', 'jeffrey.skilling@enron.com', ARRAY['kenneth.lay@enron.com'],
   'Q3 Results Discussion', 'Ken, the Q3 numbers look problematic. We need to discuss the Raptor exposures before the earnings call. Jeff',
   '2001-10-10 09:00:00+05:30', 'hash001', 'dkey001'),
  ('msg-002@enron.com', 'andrew.fastow@enron.com', ARRAY['jeffrey.skilling@enron.com'],
   'LJM Update', 'Jeff, LJM2 has absorbed another $300M in mark-to-market losses from the trading book. The Raptors are undercapitalized. Andy',
   '2001-10-11 14:30:00+05:30', 'hash002', 'dkey002'),
  ('msg-003@enron.com', 'kenneth.lay@enron.com', ARRAY['jeffrey.skilling@enron.com','andrew.fastow@enron.com'],
   'Re: Q3 Results Discussion', 'Both of you — we go ahead with the Q3 call as planned. Restate if we must but keep confidence high. Ken',
   '2001-10-12 08:15:00+05:30', 'hash003', 'dkey003'),
  ('msg-004@enron.com', 'jeffrey.skilling@enron.com', ARRAY['all.employees@enron.com'],
   'Company Outlook', 'Enron stock is the best investment you can make. Our fundamentals are strong. Jeff Skilling, CEO',
   '2001-09-26 10:00:00+05:30', 'hash004', 'dkey004'),
  ('msg-005@enron.com', 'andrew.fastow@enron.com', ARRAY['arthur.andersen@andersen.com'],
   'Raptor Accounting Treatment', 'Please confirm the accounting treatment for the Raptor SPEs remains off-balance-sheet under FAS 140. Andy',
   '2001-08-20 11:00:00+05:30', 'hash005', 'dkey005')
ON CONFLICT (message_id) DO NOTHING;

-- Claims (edges in the graph)
INSERT INTO claims (id, claim_type, subject_id, object_id, confidence, valid_from, is_current) VALUES
  ('b0000000-0000-0000-0000-000000000001', 'WORKS_AT',
   'a0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000004',
   0.99, '1997-01-01', true),
  ('b0000000-0000-0000-0000-000000000002', 'WORKS_AT',
   'a0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000004',
   0.99, '1985-01-01', true),
  ('b0000000-0000-0000-0000-000000000003', 'WORKS_AT',
   'a0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000004',
   0.99, '1990-01-01', true),
  ('b0000000-0000-0000-0000-000000000004', 'REPORTS_TO',
   'a0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000002',
   0.95, '2001-01-01', true),
  ('b0000000-0000-0000-0000-000000000005', 'REPORTS_TO',
   'a0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000001',
   0.90, '2001-01-01', true),
  ('b0000000-0000-0000-0000-000000000006', 'PARTICIPATES_IN',
   'a0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000007',
   0.92, '2001-10-16', true),
  ('b0000000-0000-0000-0000-000000000007', 'PARTICIPATES_IN',
   'a0000000-0000-0000-0000-000000000002', 'a0000000-0000-0000-0000-000000000007',
   0.88, '2001-10-16', true),
  ('b0000000-0000-0000-0000-000000000008', 'DISCUSSES',
   'a0000000-0000-0000-0000-000000000001', 'a0000000-0000-0000-0000-000000000006',
   0.91, '2001-10-10', true),
  ('b0000000-0000-0000-0000-000000000009', 'DISCUSSES',
   'a0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000005',
   0.87, '2001-10-11', true),
  ('b0000000-0000-0000-0000-000000000010', 'MENTIONS',
   'a0000000-0000-0000-0000-000000000003', 'a0000000-0000-0000-0000-000000000008',
   0.85, '2001-08-20', true)
ON CONFLICT (id) DO NOTHING;

-- Evidence linking claims back to source emails
INSERT INTO evidence (claim_id, source_type, source_id, excerpt, source_timestamp, extraction_version, confidence) VALUES
  ('b0000000-0000-0000-0000-000000000001', 'email', 'msg-001@enron.com',
   'Enron stock is the best investment you can make. Jeff Skilling, CEO',
   '2001-09-26 10:00:00+05:30', 'v1.0.0-abc123', 0.99),
  ('b0000000-0000-0000-0000-000000000004', 'email', 'msg-001@enron.com',
   'Ken, the Q3 numbers look problematic. We need to discuss the Raptor exposures before the earnings call. Jeff',
   '2001-10-10 09:00:00+05:30', 'v1.0.0-abc123', 0.95),
  ('b0000000-0000-0000-0000-000000000005', 'email', 'msg-002@enron.com',
   'LJM2 has absorbed another $300M in mark-to-market losses from the trading book.',
   '2001-10-11 14:30:00+05:30', 'v1.0.0-abc123', 0.90),
  ('b0000000-0000-0000-0000-000000000006', 'email', 'msg-003@enron.com',
   'we go ahead with the Q3 call as planned. Restate if we must but keep confidence high.',
   '2001-10-12 08:15:00+05:30', 'v1.0.0-abc123', 0.92),
  ('b0000000-0000-0000-0000-000000000008', 'email', 'msg-001@enron.com',
   'We need to discuss the Raptor exposures before the earnings call.',
   '2001-10-10 09:00:00+05:30', 'v1.0.0-abc123', 0.91),
  ('b0000000-0000-0000-0000-000000000009', 'email', 'msg-002@enron.com',
   'LJM2 has absorbed another $300M in mark-to-market losses. The Raptors are undercapitalized.',
   '2001-10-11 14:30:00+05:30', 'v1.0.0-abc123', 0.87),
  ('b0000000-0000-0000-0000-000000000010', 'email', 'msg-005@enron.com',
   'Please confirm the accounting treatment for the Raptor SPEs remains off-balance-sheet under FAS 140.',
   '2001-08-20 11:00:00+05:30', 'v1.0.0-abc123', 0.85)
ON CONFLICT DO NOTHING;

-- Verify
SELECT 'entities' AS table, COUNT(*) FROM entities
UNION ALL SELECT 'claims', COUNT(*) FROM claims
UNION ALL SELECT 'evidence', COUNT(*) FROM evidence
UNION ALL SELECT 'raw_emails', COUNT(*) FROM raw_emails;
