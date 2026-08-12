CREATE OR REPLACE VIEW AI_ADVISOR_OVERVIEW_BASE COPY GRANTS AS
-- Sample base view — self-contained so Test works out of the box.
-- Replace the VALUES with your real source, e.g.
--   SELECT ... FROM {{ source_db }}.MODELLED.<table> WHERE STATUS = 'active'
-- {{ source_db }} is defined per-env under project.yml `vars`.
SELECT * FROM VALUES
    ('5362df53', 'Category A', DATE '2026-01-01', 1200.50, 340),
    ('5362df53', 'Category B', DATE '2026-01-01',  640.00, 120),
    ('5362df53', 'Category A', DATE '2026-01-02',  980.25, 300)
    AS t(CLIENT_ID, CATEGORY, EVENT_DATE, AMOUNT, EVENTS)
;
