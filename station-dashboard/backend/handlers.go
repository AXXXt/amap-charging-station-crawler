package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type API struct {
	store *Store
}

func NewAPI(store *Store) *API {
	return &API{store: store}
}

func (a *API) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", a.health)
	mux.HandleFunc("/api/overview", a.overview)
	mux.HandleFunc("/api/stations", a.stations)
	mux.HandleFunc("/api/station", a.stationDetail)
	mux.HandleFunc("/api/station/history", a.stationHistory)
	mux.HandleFunc("/api/meta", a.meta)
	return mux
}

func (a *API) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"status": "ok",
		"time":   time.Now().Unix(),
	})
}

func (a *API) overview(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var overview Overview
	var avg sql.NullFloat64
	err := a.store.db.QueryRowContext(ctx, `
		SELECT COUNT(*),
		       SUM(CASE WHEN TRIM(COALESCE(current_price,'')) <> '' THEN 1 ELSE 0 END),
		       SUM(CASE WHEN COALESCE(JSON_LENGTH(JSON_EXTRACT(result_payload,'$.fastPrices')),0)
		                   + COALESCE(JSON_LENGTH(JSON_EXTRACT(result_payload,'$.slowPrices')),0) > 1 THEN 1 ELSE 0 END),
		       SUM(CASE WHEN COALESCE(JSON_LENGTH(JSON_EXTRACT(result_payload,'$.fastPrices')),0)
		                   + COALESCE(JSON_LENGTH(JSON_EXTRACT(result_payload,'$.slowPrices')),0) = 1 THEN 1 ELSE 0 END),
		       SUM(CASE WHEN COALESCE(JSON_LENGTH(JSON_EXTRACT(result_payload,'$.fastPrices')),0)
		                   + COALESCE(JSON_LENGTH(JSON_EXTRACT(result_payload,'$.slowPrices')),0) = 0 THEN 1 ELSE 0 END),
		       SUM(CASE WHEN TRIM(COALESCE(current_price,'')) = '' THEN 1 ELSE 0 END),
		       SUM(CASE WHEN COALESCE(JSON_LENGTH(JSON_EXTRACT(result_payload,'$.chargingPiles')),0) > 0 THEN 1 ELSE 0 END),
		       AVG(CASE WHEN NULLIF(current_price,'') REGEXP '^[0-9]+([.][0-9]+)?$'
		                THEN CAST(NULLIF(current_price,'') AS DECIMAL(10,2)) END),
		       COALESCE(MAX(received_at),0)
		  FROM site_exploration_charging_station_result`,
	).Scan(
		&overview.Total,
		&overview.Priced,
		&overview.WithTOU,
		&overview.FlatOnly,
		&overview.NoTOU,
		&overview.NoPrice,
		&overview.WithPiles,
		&avg,
		&overview.UpdatedAt,
	)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	overview.AveragePrice = round(avg.Float64, 2)
	err = a.store.db.QueryRowContext(ctx, `
		SELECT COUNT(*),
		       SUM(CASE WHEN jt.status = '空闲' THEN 1 ELSE 0 END),
		       SUM(CASE WHEN jt.status IN ('充电中','使用中','占用','已满') OR TRIM(COALESCE(jt.status,''))='' THEN 1 ELSE 0 END),
		       SUM(CASE WHEN jt.status NOT IN ('空闲','充电中','使用中','占用','已满') THEN 1 ELSE 0 END)
		  FROM site_exploration_charging_station_result r
		  JOIN JSON_TABLE(r.result_payload, '$.chargingPiles[*]' COLUMNS (
		       status VARCHAR(32) PATH '$.status'
		  )) jt
		 WHERE COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.chargingPiles')),0) > 0`,
	).Scan(&overview.PileTotal, &overview.PileIdle, &overview.PileBusy, &overview.PileUnknown)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	overview.NoTOU = overview.FlatOnly + overview.NoTOU
	if overview.Total > 0 {
		overview.TOUCoverage = round(float64(overview.WithTOU)*100/float64(overview.Total), 1)
		overview.PileCoverage = round(float64(overview.WithPiles)*100/float64(overview.Total), 1)
	}
	knownPiles := overview.PileIdle + overview.PileBusy
	if knownPiles > 0 {
		overview.IdleRate = round(float64(overview.PileIdle)*100/float64(knownPiles), 1)
		overview.BusyRate = round(float64(overview.PileBusy)*100/float64(knownPiles), 1)
	}
	writeJSON(w, http.StatusOK, overview)
}

type stationFilters struct {
	Keyword   string
	City      string
	Operator  string
	TOU       string
	PriceBand string
	Piles     string
	Sort      string
	Page      int
	PageSize  int
}

func parseStationFilters(r *http.Request) stationFilters {
	page, _ := strconv.Atoi(r.URL.Query().Get("page"))
	pageSize, _ := strconv.Atoi(r.URL.Query().Get("pageSize"))
	if page < 1 {
		page = 1
	}
	if pageSize < 10 {
		pageSize = 25
	}
	if pageSize > 100 {
		pageSize = 100
	}
	return stationFilters{
		Keyword:   strings.TrimSpace(r.URL.Query().Get("keyword")),
		City:      strings.TrimSpace(r.URL.Query().Get("city")),
		Operator:  strings.TrimSpace(r.URL.Query().Get("operator")),
		TOU:       strings.TrimSpace(r.URL.Query().Get("tou")),
		PriceBand: strings.TrimSpace(r.URL.Query().Get("priceBand")),
		Piles:     strings.TrimSpace(r.URL.Query().Get("piles")),
		Sort:      strings.TrimSpace(r.URL.Query().Get("sort")),
		Page:      page,
		PageSize:  pageSize,
	}
}

func stationWhere(f stationFilters) (string, []interface{}) {
	where := []string{"1=1"}
	args := make([]interface{}, 0, 12)
	if f.Keyword != "" {
		where = append(where, "(r.matched_station_name LIKE ? OR r.requested_name LIKE ? OR r.source_station_id LIKE ? OR r.city LIKE ?)")
		like := "%" + f.Keyword + "%"
		args = append(args, like, like, like, like)
	}
	if f.City != "" {
		where = append(where, "r.city = ?")
		args = append(args, f.City)
	}
	if f.Operator != "" {
		where = append(where, "r.operator = ?")
		args = append(args, f.Operator)
	}
	periodExpr := "COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.fastPrices')),0) + COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.slowPrices')),0)"
	switch f.TOU {
	case "has":
		where = append(where, periodExpr+" > 1")
	case "flat":
		where = append(where, periodExpr+" = 1")
	case "none":
		where = append(where, periodExpr+" = 0")
	}
	if f.PriceBand == "cheap" {
		where = append(where, "COALESCE(p.price_percentile, 1) < 0.33")
	} else if f.PriceBand == "expensive" {
		where = append(where, "COALESCE(p.price_percentile, 1) >= 0.67")
	} else if f.PriceBand == "mid" {
		where = append(where, "p.price_percentile IS NOT NULL AND p.price_percentile >= 0.33 AND p.price_percentile < 0.67")
	}
	if f.Piles == "with" {
		where = append(where, "COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.chargingPiles')),0) > 0")
	} else if f.Piles == "without" {
		where = append(where, "COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.chargingPiles')),0) = 0")
	}
	return strings.Join(where, " AND "), args
}

func stationOrder(sortBy string) string {
	switch sortBy {
	case "priceAsc":
		return "CAST(NULLIF(r.current_price,'') AS DECIMAL(10,2)) IS NULL, CAST(NULLIF(r.current_price,'') AS DECIMAL(10,2)) ASC, r.matched_station_name ASC"
	case "priceDesc":
		return "CAST(NULLIF(r.current_price,'') AS DECIMAL(10,2)) IS NULL, CAST(NULLIF(r.current_price,'') AS DECIMAL(10,2)) DESC, r.matched_station_name ASC"
	case "periods":
		return "COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.fastPrices')),0) + COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.slowPrices')),0) DESC, r.matched_station_name ASC"
	case "name":
		return "r.matched_station_name ASC"
	default:
		return "r.received_at DESC, r.matched_station_name ASC"
	}
}

func (a *API) stations(w http.ResponseWriter, r *http.Request) {
	filters := parseStationFilters(r)
	where, args := stationWhere(filters)
	base := `
		WITH priced AS (
			SELECT source_key,
			       PERCENT_RANK() OVER (ORDER BY CAST(NULLIF(current_price,'') AS DECIMAL(10,2))) AS price_percentile
			  FROM site_exploration_charging_station_result
			 WHERE NULLIF(current_price,'') REGEXP '^[0-9]+([.][0-9]+)?$'
		)`
	countSQL := base + " SELECT COUNT(*) FROM site_exploration_charging_station_result r LEFT JOIN priced p ON p.source_key=r.source_key WHERE " + where
	var total int
	if err := a.store.db.QueryRowContext(r.Context(), countSQL, args...).Scan(&total); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	listSQL := base + `
		SELECT r.source_key, r.source_station_id, r.matched_station_name, r.requested_name,
		       r.city, r.district, r.operator, r.source_address, r.collected_address,
		       COALESCE(r.current_price,''), COALESCE(r.fast_available,''), COALESCE(r.fast_total,''), COALESCE(r.fast_power,''),
		       COALESCE(r.super_available,''), COALESCE(r.super_total,''), COALESCE(r.super_power,''),
		       COALESCE(r.slow_available,''), COALESCE(r.slow_total,''), COALESCE(r.slow_power,''),
		       CAST(r.result_payload AS CHAR), COALESCE(r.captured_at,0), COALESCE(r.received_at,0), COALESCE(r.collection_source,''),
		       COALESCE(p.price_percentile,0),
		       COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.fastPrices')),0) + COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.slowPrices')),0)
		  FROM site_exploration_charging_station_result r
		  LEFT JOIN priced p ON p.source_key=r.source_key
		 WHERE ` + where + `
		 ORDER BY ` + stationOrder(filters.Sort) + `
		 LIMIT ? OFFSET ?`
	listArgs := append(append([]interface{}{}, args...), filters.PageSize, (filters.Page-1)*filters.PageSize)
	rows, err := a.store.db.QueryContext(r.Context(), listSQL, listArgs...)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	defer rows.Close()
	items := make([]Station, 0, filters.PageSize)
	for rows.Next() {
		raw, err := scanRawStation(rows)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
		items = append(items, stationFromRaw(raw))
	}
	if err := rows.Err(); err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, StationResponse{Items: items, Total: total, Page: filters.Page, PageSize: filters.PageSize})
}

type rowScanner interface {
	Scan(dest ...interface{}) error
}

func scanRawStation(scanner rowScanner) (rawStation, error) {
	var raw rawStation
	err := scanner.Scan(
		&raw.SourceKey, &raw.SourceStationID, &raw.MatchedName, &raw.RequestedName,
		&raw.City, &raw.District, &raw.Operator, &raw.SourceAddress, &raw.CollectedAddress,
		&raw.CurrentPrice, &raw.FastAvailable, &raw.FastTotal, &raw.FastPower,
		&raw.SuperAvailable, &raw.SuperTotal, &raw.SuperPower,
		&raw.SlowAvailable, &raw.SlowTotal, &raw.SlowPower,
		&raw.Payload, &raw.CapturedAt, &raw.ReceivedAt, &raw.CollectionSource,
		&raw.PricePercentile, &raw.PricePeriodCount,
	)
	return raw, err
}

func stationFromRaw(raw rawStation) Station {
	payload := decodePayload(raw.Payload)
	fastIdle, fastBusy, fastTotal := countPileGroup(payload.ChargingPiles, "fast")
	superIdle, superBusy, superTotal := countPileGroup(payload.ChargingPiles, "super")
	slowIdle, slowBusy, slowTotal := countPileGroup(payload.ChargingPiles, "slow")
	pileIdle, pileBusy, pileUnknown := countAllPiles(payload.ChargingPiles)
	if pileIdle+pileBusy+pileUnknown == 0 {
		fastTotal = maxInt(fastTotal, parseInt(raw.FastTotal))
		superTotal = maxInt(superTotal, parseInt(raw.SuperTotal))
		slowTotal = maxInt(slowTotal, parseInt(raw.SlowTotal))
		fastIdle = maxInt(fastIdle, parseInt(raw.FastAvailable))
		superIdle = maxInt(superIdle, parseInt(raw.SuperAvailable))
		slowIdle = maxInt(slowIdle, parseInt(raw.SlowAvailable))
		pileIdle = maxInt(fastIdle+superIdle+slowIdle, 0)
		pileBusy = maxInt((fastTotal-fastIdle)+(superTotal-superIdle)+(slowTotal-slowIdle), 0)
		pileUnknown = maxInt(fastTotal+superTotal+slowTotal-pileIdle-pileBusy, 0)
	}
	pileTotal := pileIdle + pileBusy + pileUnknown
	known := pileIdle + pileBusy
	idleRate := 0.0
	if known > 0 {
		idleRate = round(float64(pileIdle)*100/float64(known), 1)
	}
	price := parseFloat(raw.CurrentPrice)
	priceLevel := "missing"
	if price > 0 {
		switch {
		case raw.PricePercentile < 0.33:
			priceLevel = "cheap"
		case raw.PricePercentile >= 0.67:
			priceLevel = "expensive"
		default:
			priceLevel = "mid"
		}
	}
	address := strings.TrimSpace(raw.CollectedAddress)
	if address == "" {
		address = raw.SourceAddress
	}
	return Station{
		SourceKey: raw.SourceKey, SourceStationID: raw.SourceStationID,
		MatchedName: raw.MatchedName, RequestedName: raw.RequestedName,
		City: raw.City, District: raw.District, Operator: raw.Operator, Address: address,
		CurrentPriceText: raw.CurrentPrice, CurrentPrice: price, PricePercentile: raw.PricePercentile,
		PriceLevel: priceLevel, PricePeriodCount: raw.PricePeriodCount,
		HasTOU: raw.PricePeriodCount > 1, FlatOnly: raw.PricePeriodCount == 1,
		NoPriceData: raw.PricePeriodCount == 0 && strings.TrimSpace(raw.CurrentPrice) == "",
		FastIdle:    fastIdle, FastBusy: fastBusy, FastTotal: fastTotal,
		SuperIdle: superIdle, SuperBusy: superBusy, SuperTotal: superTotal,
		SlowIdle: slowIdle, SlowBusy: slowBusy, SlowTotal: slowTotal,
		PileIdle: pileIdle, PileBusy: pileBusy, PileUnknown: pileUnknown, PileTotal: pileTotal,
		IdleRate: idleRate, CapturedAt: raw.CapturedAt, ReceivedAt: raw.ReceivedAt,
		CollectionSource: raw.CollectionSource, FastPower: raw.FastPower,
		SuperPower: raw.SuperPower, SlowPower: raw.SlowPower,
	}
}

func (a *API) stationDetail(w http.ResponseWriter, r *http.Request) {
	sourceKey := strings.TrimSpace(r.URL.Query().Get("sourceKey"))
	if sourceKey == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "sourceKey is required"})
		return
	}
	row := a.store.db.QueryRowContext(r.Context(), `
		SELECT r.source_key, r.source_station_id, r.matched_station_name, r.requested_name,
		       r.city, r.district, r.operator, r.source_address, r.collected_address,
		       COALESCE(r.current_price,''), COALESCE(r.fast_available,''), COALESCE(r.fast_total,''), COALESCE(r.fast_power,''),
		       COALESCE(r.super_available,''), COALESCE(r.super_total,''), COALESCE(r.super_power,''),
		       COALESCE(r.slow_available,''), COALESCE(r.slow_total,''), COALESCE(r.slow_power,''),
		       CAST(r.result_payload AS CHAR), COALESCE(r.captured_at,0), COALESCE(r.received_at,0), COALESCE(r.collection_source,''),
		       0,
		       COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.fastPrices')),0) + COALESCE(JSON_LENGTH(JSON_EXTRACT(r.result_payload,'$.slowPrices')),0)
		  FROM site_exploration_charging_station_result r
		 WHERE r.source_key = ?`, sourceKey)
	raw, err := scanRawStation(row)
	if err != nil {
		if err == sql.ErrNoRows {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "station not found"})
			return
		}
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	payload := decodePayload(raw.Payload)
	base := stationFromRaw(raw)
	detail := StationDetail{
		Station:       base,
		BusinessHours: payload.BusinessHours,
		ParkingFee:    payload.ParkingFee,
		FastPrices:    payload.FastPrices,
		SlowPrices:    payload.SlowPrices,
		Piles:         payload.ChargingPiles,
		Payload:       decodeMap(raw.Payload),
		ServedAt:      time.Now().Unix(),
	}
	writeJSON(w, http.StatusOK, detail)
}

func (a *API) stationHistory(w http.ResponseWriter, r *http.Request) {
	sourceKey := strings.TrimSpace(r.URL.Query().Get("sourceKey"))
	if sourceKey == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "sourceKey is required"})
		return
	}
	limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
	if limit < 1 || limit > 300 {
		limit = 120
	}
	rows, err := a.store.db.QueryContext(r.Context(), `
		SELECT COALESCE(captured_at,0), COALESCE(received_at,0), COALESCE(price_json,'{}'), COALESCE(availability_json,'{}'), COALESCE(is_changed,1)
		  FROM site_exploration_charging_station_dynamic_history
		 WHERE source_key = ?
		 ORDER BY captured_at DESC, id DESC
		 LIMIT ?`, sourceKey, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	defer rows.Close()
	points := make([]HistoryPoint, 0, limit)
	for rows.Next() {
		var point HistoryPoint
		var priceJSON, availabilityJSON string
		if err := rows.Scan(&point.CapturedAt, &point.ReceivedAt, &priceJSON, &availabilityJSON, &point.IsChanged); err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
		var pricePayload map[string]interface{}
		var availability map[string]interface{}
		_ = json.Unmarshal([]byte(priceJSON), &pricePayload)
		_ = json.Unmarshal([]byte(availabilityJSON), &availability)
		point.CurrentPrice = parseFloat(fmt.Sprint(pricePayload["currentPrice"]))
		if pile, ok := availability["pile"].(map[string]interface{}); ok {
			point.PileIdle = intFromAny(pile["idle"])
			point.PileBusy = intFromAny(pile["busy"])
			point.PileUnknown = intFromAny(pile["unknown"])
			point.PileTotal = intFromAny(pile["total"])
		}
		points = append(points, point)
	}
	for i, j := 0, len(points)-1; i < j; i, j = i+1, j-1 {
		points[i], points[j] = points[j], points[i]
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"items": points})
}

func (a *API) meta(w http.ResponseWriter, r *http.Request) {
	meta := FilterMeta{Cities: []Facet{}, Operators: []Facet{}}
	rows, err := a.store.db.QueryContext(r.Context(), `
		SELECT city, COUNT(*) FROM site_exploration_charging_station_result
		 WHERE TRIM(COALESCE(city,'')) <> '' GROUP BY city ORDER BY COUNT(*) DESC, city LIMIT 200`)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	for rows.Next() {
		var facet Facet
		if err := rows.Scan(&facet.Name, &facet.Count); err != nil {
			_ = rows.Close()
			writeError(w, http.StatusInternalServerError, err)
			return
		}
		meta.Cities = append(meta.Cities, facet)
	}
	_ = rows.Close()
	rows, err = a.store.db.QueryContext(r.Context(), `
		SELECT operator, COUNT(*) FROM site_exploration_charging_station_result
		 WHERE TRIM(COALESCE(operator,'')) <> '' GROUP BY operator ORDER BY COUNT(*) DESC, operator LIMIT 200`)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	defer rows.Close()
	for rows.Next() {
		var facet Facet
		if err := rows.Scan(&facet.Name, &facet.Count); err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
		meta.Operators = append(meta.Operators, facet)
	}
	writeJSON(w, http.StatusOK, meta)
}

func countPileGroup(piles []PileDetail, group string) (idle, busy, total int) {
	for _, pile := range piles {
		kind := "fast"
		if strings.Contains(pile.ChargingType, "超") {
			kind = "super"
		} else if strings.Contains(pile.ChargingType, "慢") {
			kind = "slow"
		}
		if kind != group {
			continue
		}
		total++
		switch strings.TrimSpace(pile.Status) {
		case "空闲", "空":
			idle++
		case "充电中", "使用中", "占用", "已满":
			busy++
		case "":
			busy++
		}
	}
	return idle, busy, total
}

func countAllPiles(piles []PileDetail) (idle, busy, unknown int) {
	for _, pile := range piles {
		switch strings.TrimSpace(pile.Status) {
		case "空闲", "空":
			idle++
		case "充电中", "使用中", "占用", "已满":
			busy++
		case "":
			busy++
		default:
			unknown++
		}
	}
	return idle, busy, unknown
}

func parseInt(value string) int {
	number, _ := strconv.Atoi(strings.TrimSpace(value))
	return number
}

func intFromAny(value interface{}) int {
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	case json.Number:
		number, _ := typed.Int64()
		return int(number)
	default:
		return parseInt(fmt.Sprint(value))
	}
}

func parseFloat(value string) float64 {
	number, _ := strconv.ParseFloat(strings.TrimSpace(value), 64)
	return number
}

func round(value float64, digits int) float64 {
	scale := math.Pow10(digits)
	return math.Round(value*scale) / scale
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func writeJSON(w http.ResponseWriter, status int, value interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]string{"error": err.Error()})
}
