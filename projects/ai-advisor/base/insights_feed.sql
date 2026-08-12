CREATE OR REPLACE VIEW INSIGHTS_FEED COPY GRANTS AS
-- Latest-snapshot insights & recommendations per client, ALL categories
-- (overview / job / recruiter / cost_center). Reproduces the LookML
-- `insights_and_recommendations` view; CATEGORY is a dimension so each surface
-- filters it (overview widget -> 'overview'; job insight panel/chips -> 'job').
-- priority_label follows the LookML CASE (>=4 high, >=3 medium, else low).
-- Row-grain: query DIMENSIONS at INSIGHT_ID grain (console compiles no FACTS).
WITH max_date AS (
    SELECT CLIENT_ID, MAX(DATE(DATE_ADDED)) AS d
    FROM {{ source_db }}.MODELLED.UA_INSIGHTS_AND_RECOMMENDATIONS
    GROUP BY CLIENT_ID
)
SELECT
    i.CLIENT_ID,
    -- UUID is null in this table; derive a stable non-null row key.
    MD5(CONCAT_WS('|', i.CLIENT_ID, COALESCE(i.ID, ''), COALESCE(i.CATEGORY, ''),
        COALESCE(i.DIMENSION, ''), COALESCE(i.DIMENSION_VALUE, ''), COALESCE(i.TITLE, ''),
        COALESCE(i.ORDER_IDS, ''))) AS ROW_ID,
    i.ID                            AS INSIGHT_ID,
    i.CATEGORY,
    i.ENTITY,
    i.DIMENSION,
    i.DIMENSION_VALUE,
    i.TITLE,
    i.DESCRIPTION,
    i.WHAT_HAPPENED,
    i.WHY_HAPPENED,
    i.WHY_HAPPENED_V2,
    i.RECOMMENDATIONS,
    i.PRIORITY,
    CASE
        WHEN TRY_TO_NUMBER(i.PRIORITY) >= 4 THEN 'high'
        WHEN TRY_TO_NUMBER(i.PRIORITY) >= 3 THEN 'medium'
        ELSE 'low'
    END                            AS PRIORITY_LABEL,
    i.TAG,
    i.TAGS,
    i.FOCUS_AREAS,
    i.ORDER_IDS,
    i.CLIENT_ORDER_MAP,
    i.COST_CENTER_ORDER_MAP,
    i.EXPECTED_OUTCOME,
    i.TOTAL_ORDERS,
    i.ADDITIONAL_CANDIDATES_NEEDED,
    i.USER_ID
FROM {{ source_db }}.MODELLED.UA_INSIGHTS_AND_RECOMMENDATIONS i
JOIN max_date m ON i.CLIENT_ID = m.CLIENT_ID AND DATE(i.DATE_ADDED) = m.d
;
