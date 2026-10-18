-- A visitor left with nothing, or you need to see what really happened.
--
-- This is the bug path. The booth writes the SESSIONS row at the postbox, so if the
-- QR failed, the browser died, or the document did not stage, the row is usually
-- still there and the blueprint can be handed over by other means.

-- 1. Find them. Any of these will do - people rarely remember more than a name.
SELECT SESSION_ID, SESSION_TS, FIRST_NAME, COMPANY, INDUSTRY, POC_NAME,
       DELIVERY_STATUS, DOCUMENT_URL
FROM LOCO4COCO.BOOTH.SESSIONS
WHERE UPPER(FIRST_NAME) LIKE UPPER('%name%')      -- change me
   OR UPPER(COMPANY)    LIKE UPPER('%company%')   -- or me
ORDER BY SESSION_TS DESC;

-- The last visitor, when they are still standing in front of you.
SELECT * FROM LOCO4COCO.BOOTH.SESSIONS ORDER BY SESSION_TS DESC LIMIT 1;

-- 2. Rebuild what they should have received, without the document. Everything the
--    takeaway contained is in these columns, so this can be pasted into an email.
SELECT FIRST_NAME, COMPANY,
       PROBLEM_STATEMENT,
       POC_NAME,
       POC_SUMMARY,
       POC_ARCHETYPE,
       FEATURES,
       CONSIDERATIONS,
       FIRST_STEP,
       MARKETPLACE_JOINED,
       DATA_HELD,
       GUIDE_URL,
       DOCUMENT_URL
FROM LOCO4COCO.BOOTH.SESSIONS
WHERE SESSION_ID = 'paste-session-id-here';       -- change me

-- 3. Re-issue the download link. Presigned URLs expire after seven days, so a
--    visitor coming back later needs a fresh one. List the stage, find their file,
--    then sign it again.
LIST @LOCO4COCO.BOOTH.BLUEPRINTS;

SELECT GET_PRESIGNED_URL(@LOCO4COCO.BOOTH.BLUEPRINTS, 'paste-filename-here', 604800);

-- 4. Read the conversation, when the outcome looks wrong and you want to know why.
--    One row per exchange, both sides of it.
SELECT t.TURN_TS, t.LOCATION, t.VISITOR_INPUT, t.REPLY,
       t.DURATION_SECONDS, t.SUCCEEDED
FROM LOCO4COCO.BOOTH.TURNS t
WHERE t.SESSION_ID = 'paste-session-id-here'      -- change me
ORDER BY t.TURN_TS;

-- 5. Visitors who did NOT get their handover. Work this list at the end of the day
--    while you can still put a face to the name.
SELECT SESSION_TS, FIRST_NAME, COMPANY, DELIVERY_STATUS, POC_NAME, DOCUMENT_URL
FROM LOCO4COCO.BOOTH.SESSIONS
WHERE DELIVERY_STATUS IS DISTINCT FROM 'DELIVERED'
  -- NOT_QUALIFIED visitors were deliberately given no document (SE scoring).
  AND DELIVERY_STATUS IS DISTINCT FROM 'NOT_QUALIFIED'
ORDER BY SESSION_TS DESC;

-- 6. Sessions that started and never finished - a row with no blueprint. Usually
--    someone who walked away mid-visit, occasionally a crash worth investigating
--    in the turns above.
SELECT SESSION_TS, FIRST_NAME, COMPANY, INDUSTRY, POC_ARCHETYPE, DURATION_SECONDS
FROM LOCO4COCO.BOOTH.SESSIONS
WHERE COALESCE(POC_NAME, '') = ''
ORDER BY SESSION_TS DESC;
