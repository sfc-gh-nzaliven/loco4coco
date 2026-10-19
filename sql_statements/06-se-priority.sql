-- SE prioritisation: today's use cases ranked by SE score.
--
-- Each visitor gets three 1-5 scores at the postbox (never shown to them):
--   SE_SCORE_NOW          can they work on it in the near term (their own answer)
--   SE_SCORE_SOLVABLE     Snowflake-solvable, and a quick win CoCo can build
--   SE_SCORE_CONSUMPTION  estimated Snowflake consumption
-- SE_QUALIFIED is TRUE when the average is >= 2.5 (in practice a total of 8+/15); those visitors were told to
-- come and talk to an SE. SE_BRIEF is a 20-second read before walking over;
-- SE_SCORING holds the per-score reasons and the $ estimate.
-- SCORED_BY_MODEL = FALSE means the model call failed and the visitor was let
-- through on fallback scores - re-read those rows by hand.

SELECT SESSION_TS,
       FIRST_NAME,
       COMPANY,
       INDUSTRY,
       SE_SCORE_AVG,
       SE_SCORE_NOW,
       SE_SCORE_SOLVABLE,
       SE_SCORE_CONSUMPTION,
       SE_QUALIFIED,
       SE_BRIEF,
       SE_SCORING:capacity::TEXT             AS CAPACITY_ANSWER,
       SE_SCORING:consumption_estimate::TEXT AS CONSUMPTION_ESTIMATE,
       SE_SCORING:solvable_why::TEXT         AS SOLVABLE_WHY,
       SE_SCORING:consumption_why::TEXT      AS CONSUMPTION_WHY,
       SE_SCORING:scored_by_model::BOOLEAN   AS SCORED_BY_MODEL,
       PROBLEM_STATEMENT,
       POC_NAME,
       DOCUMENT_URL
FROM LOCO4COCO.BOOTH.SESSIONS
WHERE SESSION_TS >= DATE_TRUNC('day', CURRENT_TIMESTAMP())
  AND SE_SCORE_AVG IS NOT NULL
ORDER BY SE_QUALIFIED DESC, SE_SCORE_AVG DESC, SE_SCORE_CONSUMPTION DESC;
