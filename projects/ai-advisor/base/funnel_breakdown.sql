CREATE OR REPLACE VIEW FUNNEL_BREAKDOWN COPY GRANTS AS
-- Candidate count per hiring-funnel stage across the client's latest OPEN orders,
-- with each stage's canonical order (Withdrawn sinks to 99, as in the LookML
-- `hiring_funnel` sort_by). One row per (order, stage). SV_FUNNEL sums CANDIDATE_COUNT
-- by STAGE (ordered by STAGE_ORDER) for the Pipeline funnel; exclude 'Withdrawn'
-- for the pipeline candidate total.
WITH max_date AS (
    SELECT CLIENT_ID, MAX(DATE(DATE_ADDED)) AS d
    FROM {{ source_db }}.MODELLED.UA_PREDICTIVE_ANALYTICS_JOB_MERTICS_PREDICTIONS
    WHERE IS_ORDER_OPEN = TRUE
    GROUP BY CLIENT_ID
),
latest AS (
    SELECT j.CLIENT_ID, j.ORDER_ID, j.FUNNEL_STAGES, j.FUNNEL_STAGES_ORDER
    FROM {{ source_db }}.MODELLED.UA_PREDICTIVE_ANALYTICS_JOB_MERTICS_PREDICTIONS j
    JOIN max_date m ON j.CLIENT_ID = m.CLIENT_ID AND DATE(j.DATE_ADDED) = m.d
    WHERE j.IS_ORDER_OPEN = TRUE
)
SELECT
    j.CLIENT_ID,
    j.ORDER_ID,
    j.ORDER_ID || '|' || s.key::string           AS FUNNEL_ROW_ID,
    s.key::string                                AS STAGE,
    COALESCE(j.FUNNEL_STAGES_ORDER[s.key]::int, 99) AS STAGE_ORDER,
    s.value::int                                 AS CANDIDATE_COUNT
FROM latest j,
     LATERAL FLATTEN(input => j.FUNNEL_STAGES) s
;
