package com.pinkline.kafkabridge.processor;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.fasterxml.jackson.dataformat.xml.XmlMapper;
import com.pinkline.kafkabridge.model.ATRTimeTable;
import com.pinkline.kafkabridge.model.RouteInfo;
import com.pinkline.kafkabridge.model.SingleArrival;
import com.pinkline.kafkabridge.model.SingleDeparture;
import org.apache.camel.Exchange;
import org.apache.camel.Processor;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.TimeZone;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * XmlToJsonProcessor
 *
 * HOW IT WORKS — Jackson does everything automatically:
 *
 *   1. XmlMapper reads XML string
 *   2. Fills Java POJO (ATRTimeTable / SingleArrival / SingleDeparture / RouteInfo)
 *   3. ObjectMapper converts POJO to ICD-compliant JSON
 *
 * NO manual DOM parsing. NO XPath. NO string manipulation.
 * Jackson handles it all.
 *
 * Flags (ATRTimeTable Evts.F bitmask — from RegulationMsgParser.java):
 *   0x01 = ARRIVAL_VALID
 *   0x02 = DEPARTURE_VALID
 *   0x04 = HISTORY_ITEM  → skip
 *   0x20 = PASS_EVENT    → skip
 */
public class XmlToJsonProcessor implements Processor {

    private static final Logger log = LoggerFactory.getLogger(XmlToJsonProcessor.class);

    // Jackson XML reader — reads XML → POJO automatically
    private static final XmlMapper xmlMapper = new XmlMapper();

    // Jackson JSON writer — converts POJO → JSON
    private static final ObjectMapper jsonMapper = new ObjectMapper();

    // Rolling health sequence counter 0–255
    private static final AtomicInteger healthSeq = new AtomicInteger(0);

    // Flags from RegulationMsgParser.java
    private static final int ARRIVAL_VALID   = 0x01;
    private static final int DEPARTURE_VALID = 0x02;
    private static final int HISTORY_ITEM    = 0x04;
    private static final int PASS_EVENT      = 0x20;

    @Override
    public void process(Exchange exchange) throws Exception {

        String xml    = exchange.getIn().getBody(String.class);
        String schema = exchange.getIn().getHeader("rcsschema", String.class);

        if (xml == null || xml.isBlank()) {
            throw new IllegalArgumentException("Empty XML message received");
        }

        // Detect message type from XML root element (fast string check)
        String json;
        if (xml.contains("<ATRTimeTable")) {
            // Timetable — topic TMS.PISInfo
            ATRTimeTable timetable = xmlMapper.readValue(xml, ATRTimeTable.class);
            json = buildFromATRTimeTable(timetable, schema);

        } else if (xml.contains("<SingleArrival")) {
            // Train arrived at platform — topic RCS.E2K.TMS.TrafficReportClient
            SingleArrival arrival = xmlMapper.readValue(xml, SingleArrival.class);
            json = buildFromSingleArrival(arrival);

        } else if (xml.contains("<SingleDeparture")) {
            // Train departed platform — topic RCS.E2K.TMS.TrafficReportClient
            SingleDeparture departure = xmlMapper.readValue(xml, SingleDeparture.class);
            json = buildFromSingleDeparture(departure);

        } else if (xml.contains("<routeinfo")) {
            // Route info — topic RCS.E2K.TMS.RouteInfo
            RouteInfo routeInfo = xmlMapper.readValue(xml, RouteInfo.class);
            json = buildFromRouteInfo(routeInfo);

        } else {
            log.warn("Unknown XML message type — building minimal JSON envelope");
            json = buildMinimalJson();
        }

        exchange.getIn().setBody(json);
        exchange.getIn().setHeader("Content-Type", "application/json");
        log.debug("XML converted to JSON successfully");
    }

    // ══════════════════════════════════════════════════════════════════════
    // ATRTimeTable → ICD JSON
    // Jackson read the XML into ATRTimeTable POJO.
    // Now build ICD-compliant platformPredictions from Tg/Evts data.
    // ══════════════════════════════════════════════════════════════════════
    private String buildFromATRTimeTable(ATRTimeTable timetable, String schema) throws Exception {

        SimpleDateFormat inFmt = new SimpleDateFormat("yyyyMMdd'T'HHmmss");
        inFmt.setTimeZone(TimeZone.getTimeZone("UTC"));
        SimpleDateFormat hhmm = new SimpleDateFormat("HH:mm:ss");
        hhmm.setTimeZone(TimeZone.getTimeZone("UTC"));

        long nowMs = System.currentTimeMillis();

        // Parse base date — all Evts times (As, Ds) are seconds from this
        Date baseDate = null;
        if (timetable.dateTime != null && !timetable.dateTime.isBlank()) {
            baseDate = inFmt.parse(timetable.dateTime);
        }

        ObjectNode root = buildEnvelope(nowMs);
        ArrayNode platformPredictions = jsonMapper.createArrayNode();

        if (timetable.trains != null && timetable.trains.tg != null) {

            for (ATRTimeTable.Tg tg : timetable.trains.tg) {

                // Train ID: use TTGUID (V3) or Id (V1/V2)
                String trainIdStr = (tg.ttGuid != null && !tg.ttGuid.isBlank())
                        ? tg.ttGuid : (tg.id != null ? tg.id : "0");

                // TmsPTI can override TTGUID
                if (tg.tmsPti != null && tg.tmsPti.trGuid != null && !tg.tmsPti.trGuid.isBlank()) {
                    trainIdStr = tg.tmsPti.trGuid;
                }

                int tripNumber = tg.tripNo;
                if (tg.ctd != null && tg.ctd.tn > 0) tripNumber = tg.ctd.tn;
                int destPlatformId = tg.ctd != null ? tg.ctd.lpid : 0;

                if (tg.evts == null) continue;

                for (ATRTimeTable.Evts evt : tg.evts) {

                    // Skip history and pass-through events (same logic as RegulationMsgParser)
                    if ((evt.flags & HISTORY_ITEM) != 0) continue;
                    if ((evt.flags & PASS_EVENT)   != 0) continue;
                    if ((evt.flags & ARRIVAL_VALID) == 0 && (evt.flags & DEPARTURE_VALID) == 0) continue;

                    // Calculate absolute times: baseDate + seconds offset
                    long arrMs = baseDate != null ? baseDate.getTime() + (evt.arrivalSecs * 1000L) : 0;
                    long depMs = baseDate != null ? baseDate.getTime() + (evt.departureSecs * 1000L) : 0;

                    // arrivalTime = seconds from now, clamped to 0 (matches ScadaMsgCreator)
                    long arrivalSecsFromNow = Math.max(0, (arrMs - nowMs) / 1000L);
                    String departureTime = baseDate != null ? hhmm.format(new Date(depMs)) : "";

                    // Build platform entry
                    ObjectNode platform = jsonMapper.createObjectNode();
                    platform.put("platformId", "PL" + evt.platformTmsId);

                    ObjectNode train = jsonMapper.createObjectNode();
                    train.put("slot",          1);
                    train.put("trainId",       trainIdStr);
                    train.put("arrivalTime",   arrivalSecsFromNow);
                    train.put("departureTime", departureTime);
                    train.put("status",        0);   // 0=predicted
                    train.put("destination",   "PL" + destPlatformId);
                    train.put("serviceState",  1);   // 1=will stop
                    train.put("runNumber",     tripNumber);

                    ArrayNode trains = jsonMapper.createArrayNode();
                    trains.add(train);
                    platform.set("predictedTrains", trains);
                    platformPredictions.add(platform);
                }
            }
        }

        root.set("platformPredictions", platformPredictions);
        root.set("blockOccupancies",    jsonMapper.createArrayNode());
        root.set("gateCommands",        jsonMapper.createArrayNode());
        return root.toString();
    }

    // ══════════════════════════════════════════════════════════════════════
    // SingleArrival → ICD JSON
    // Jackson already filled the POJO. Just map to ICD fields.
    // ══════════════════════════════════════════════════════════════════════
    private String buildFromSingleArrival(SingleArrival msg) throws Exception {
        if (msg.arr == null) return buildMinimalJson();

        String trainId = resolveTrainId(
                msg.arr.train != null ? msg.arr.train.ttGuid : null,
                msg.arr.train != null ? msg.arr.train.id : null);
        int platformId = msg.arr.loc != null ? msg.arr.loc.tmsId : 0;
        String oTime   = msg.arr.atimes != null ? msg.arr.atimes.oTime : null;

        long nowMs = System.currentTimeMillis();
        long arrSecs = calcSecsFromNow(oTime, nowMs);

        ObjectNode root = buildEnvelope(nowMs);
        root.set("platformPredictions", buildSinglePlatform(
                platformId, trainId, arrSecs, "", 1 /* at platform */));
        root.set("blockOccupancies", jsonMapper.createArrayNode());
        root.set("gateCommands",     jsonMapper.createArrayNode());
        return root.toString();
    }

    // ══════════════════════════════════════════════════════════════════════
    // SingleDeparture → ICD JSON
    // ══════════════════════════════════════════════════════════════════════
    private String buildFromSingleDeparture(SingleDeparture msg) throws Exception {
        if (msg.dep == null) return buildMinimalJson();

        String trainId = resolveTrainId(
                msg.dep.train != null ? msg.dep.train.ttGuid : null,
                msg.dep.train != null ? msg.dep.train.id : null);
        int platformId   = msg.dep.loc    != null ? msg.dep.loc.tmsId     : 0;
        String oTime     = msg.dep.dtimes != null ? msg.dep.dtimes.oTime  : null;

        long nowMs = System.currentTimeMillis();
        String depTime = formatTime(oTime);

        ObjectNode root = buildEnvelope(nowMs);
        root.set("platformPredictions", buildSinglePlatform(
                platformId, trainId, 0, depTime, 2 /* departed */));
        root.set("blockOccupancies", jsonMapper.createArrayNode());
        root.set("gateCommands",     jsonMapper.createArrayNode());
        return root.toString();
    }

    // ══════════════════════════════════════════════════════════════════════
    // RouteInfo → ICD JSON
    // ══════════════════════════════════════════════════════════════════════
    private String buildFromRouteInfo(RouteInfo msg) throws Exception {
        long nowMs = System.currentTimeMillis();

        ArrayNode destinations = jsonMapper.createArrayNode();
        if (msg.dests != null && msg.dests.dest != null) {
            for (RouteInfo.Dest d : msg.dests.dest) {
                destinations.add("PL" + d.tmsId);
            }
        }

        ObjectNode routeData = jsonMapper.createObjectNode();
        routeData.put("trainId",   msg.trainId  != null ? msg.trainId  : "");
        routeData.put("trainGUID", msg.ttGuid   != null ? msg.ttGuid   : "");
        routeData.set("route", destinations);

        ObjectNode root = buildEnvelope(nowMs);
        root.set("trains",              jsonMapper.createArrayNode());
        root.set("platformPredictions", jsonMapper.createArrayNode());
        root.set("blockOccupancies",    jsonMapper.createArrayNode());
        root.set("gateCommands",        jsonMapper.createArrayNode());
        root.set("routeInfo",           routeData);
        return root.toString();
    }

    // ── Helpers ────────────────────────────────────────────────────────────

    /** Build the standard ICD JSON envelope (matches ScadaMsgCreator.java) */
    private ObjectNode buildEnvelope(long nowMs) throws Exception {
        SimpleDateFormat tsFmt = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'");
        tsFmt.setTimeZone(TimeZone.getTimeZone("UTC"));
        int seq = healthSeq.getAndUpdate(v -> (v + 1) & 0xFF);

        ObjectNode root = jsonMapper.createObjectNode();
        root.put("schemaVersion", "1.0");
        root.put("messageType",   "TMS_PAS_UPDATE");
        root.put("timestamp",     tsFmt.format(new Date(nowMs)));

        ObjectNode header = jsonMapper.createObjectNode();
        header.put("server",          "RCS.E2K.PIS");
        header.put("version",         "1.0");
        header.put("health",          1);
        header.put("healthSeq",       seq);
        header.put("statusUpdateAll", false);
        root.set("header", header);

        root.set("trains", jsonMapper.createArrayNode());
        return root;
    }

    /** Build a single platform prediction array */
    private ArrayNode buildSinglePlatform(int platformId, String trainId,
                                           long arrivalSecs, String departureTime,
                                           int status) {
        ObjectNode train = jsonMapper.createObjectNode();
        train.put("slot",          1);
        train.put("trainId",       trainId);
        train.put("arrivalTime",   arrivalSecs);
        train.put("departureTime", departureTime);
        train.put("status",        status);
        train.put("destination",   "");
        train.put("serviceState",  1);
        train.put("runNumber",     0);

        ArrayNode trains = jsonMapper.createArrayNode();
        trains.add(train);

        ObjectNode platform = jsonMapper.createObjectNode();
        platform.put("platformId", "PL" + platformId);
        platform.set("predictedTrains", trains);

        ArrayNode result = jsonMapper.createArrayNode();
        result.add(platform);
        return result;
    }

    /** Use TTGUID if present, fall back to id */
    private String resolveTrainId(String ttGuid, String id) {
        if (ttGuid != null && !ttGuid.isBlank()) return ttGuid;
        if (id     != null && !id.isBlank())     return id;
        return "0";
    }

    /** Parse TMS time string "yyyyMMdd'T'HHmmss" → seconds from now */
    private long calcSecsFromNow(String oTime, long nowMs) {
        if (oTime == null || oTime.isBlank()) return 0;
        try {
            SimpleDateFormat fmt = new SimpleDateFormat("yyyyMMdd'T'HHmmss");
            fmt.setTimeZone(TimeZone.getTimeZone("UTC"));
            Date t = fmt.parse(oTime);
            return Math.max(0, (t.getTime() - nowMs) / 1000L);
        } catch (Exception e) { return 0; }
    }

    /** Parse TMS time string → "HH:mm:ss" */
    private String formatTime(String oTime) {
        if (oTime == null || oTime.isBlank()) return "";
        try {
            SimpleDateFormat in  = new SimpleDateFormat("yyyyMMdd'T'HHmmss");
            SimpleDateFormat out = new SimpleDateFormat("HH:mm:ss");
            in.setTimeZone(TimeZone.getTimeZone("UTC"));
            out.setTimeZone(TimeZone.getTimeZone("UTC"));
            return out.format(in.parse(oTime));
        } catch (Exception e) { return ""; }
    }

    /** Minimal JSON when message type is unknown */
    private String buildMinimalJson() throws Exception {
        ObjectNode root = buildEnvelope(System.currentTimeMillis());
        root.set("platformPredictions", jsonMapper.createArrayNode());
        root.set("blockOccupancies",    jsonMapper.createArrayNode());
        root.set("gateCommands",        jsonMapper.createArrayNode());
        return root.toString();
    }
}
