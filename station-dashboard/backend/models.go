package main

import "encoding/json"

type Overview struct {
	Total        int     `json:"total"`
	Priced       int     `json:"priced"`
	WithTOU      int     `json:"withTou"`
	FlatOnly     int     `json:"flatOnly"`
	NoTOU        int     `json:"noTou"`
	NoPrice      int     `json:"noPrice"`
	WithPiles    int     `json:"withPiles"`
	AveragePrice float64 `json:"averagePrice"`
	PileTotal    int     `json:"pileTotal"`
	PileIdle     int     `json:"pileIdle"`
	PileBusy     int     `json:"pileBusy"`
	PileUnknown  int     `json:"pileUnknown"`
	UpdatedAt    int64   `json:"updatedAt"`
	TOUCoverage  float64 `json:"touCoverage"`
	PileCoverage float64 `json:"pileCoverage"`
	IdleRate     float64 `json:"idleRate"`
	BusyRate     float64 `json:"busyRate"`
}

type Station struct {
	SourceKey        string  `json:"sourceKey"`
	SourceStationID  string  `json:"sourceStationId"`
	MatchedName      string  `json:"matchedName"`
	RequestedName    string  `json:"requestedName"`
	City             string  `json:"city"`
	District         string  `json:"district"`
	Operator         string  `json:"operator"`
	Address          string  `json:"address"`
	CurrentPriceText string  `json:"currentPriceText"`
	CurrentPrice     float64 `json:"currentPrice"`
	PricePercentile  float64 `json:"pricePercentile"`
	PriceLevel       string  `json:"priceLevel"`
	PricePeriodCount int     `json:"pricePeriodCount"`
	HasTOU           bool    `json:"hasTou"`
	FlatOnly         bool    `json:"flatOnly"`
	NoPriceData      bool    `json:"noPriceData"`
	FastIdle         int     `json:"fastIdle"`
	FastBusy         int     `json:"fastBusy"`
	FastTotal        int     `json:"fastTotal"`
	SuperIdle        int     `json:"superIdle"`
	SuperBusy        int     `json:"superBusy"`
	SuperTotal       int     `json:"superTotal"`
	SlowIdle         int     `json:"slowIdle"`
	SlowBusy         int     `json:"slowBusy"`
	SlowTotal        int     `json:"slowTotal"`
	PileIdle         int     `json:"pileIdle"`
	PileBusy         int     `json:"pileBusy"`
	PileUnknown      int     `json:"pileUnknown"`
	PileTotal        int     `json:"pileTotal"`
	IdleRate         float64 `json:"idleRate"`
	CapturedAt       int64   `json:"capturedAt"`
	ReceivedAt       int64   `json:"receivedAt"`
	CollectionSource string  `json:"collectionSource"`
	FastPower        string  `json:"fastPower"`
	SuperPower       string  `json:"superPower"`
	SlowPower        string  `json:"slowPower"`
}

type PricePeriod struct {
	Time       string `json:"time"`
	ElecFee    string `json:"elecFee"`
	ServiceFee string `json:"serviceFee"`
	TotalPrice string `json:"totalPrice"`
	Tag        string `json:"tag"`
}

type PileDetail struct {
	DeviceID      string `json:"deviceId"`
	ChargingType  string `json:"chargingType"`
	EquipmentType string `json:"equipmentType"`
	RatedPower    string `json:"ratedPower"`
	RatedCurrent  string `json:"ratedCurrent"`
	RatedVoltage  string `json:"ratedVoltage"`
	Standard      string `json:"standard"`
	Status        string `json:"status"`
}

type StationDetail struct {
	Station
	BusinessHours string                 `json:"businessHours"`
	ParkingFee    string                 `json:"parkingFee"`
	FastPrices    []PricePeriod          `json:"fastPrices"`
	SlowPrices    []PricePeriod          `json:"slowPrices"`
	Piles         []PileDetail           `json:"piles"`
	Payload       map[string]interface{} `json:"payload"`
	ServedAt      int64                  `json:"servedAt"`
}

type HistoryPoint struct {
	CapturedAt   int64   `json:"capturedAt"`
	ReceivedAt   int64   `json:"receivedAt"`
	CurrentPrice float64 `json:"currentPrice"`
	PileIdle     int     `json:"pileIdle"`
	PileBusy     int     `json:"pileBusy"`
	PileUnknown  int     `json:"pileUnknown"`
	PileTotal    int     `json:"pileTotal"`
	IsChanged    bool    `json:"isChanged"`
}

type StationResponse struct {
	Items    []Station `json:"items"`
	Total    int       `json:"total"`
	Page     int       `json:"page"`
	PageSize int       `json:"pageSize"`
}

type FilterMeta struct {
	Cities    []Facet `json:"cities"`
	Operators []Facet `json:"operators"`
}

type Facet struct {
	Name  string `json:"name"`
	Count int    `json:"count"`
}

type rawStation struct {
	SourceKey        string
	SourceStationID  string
	MatchedName      string
	RequestedName    string
	City             string
	District         string
	Operator         string
	SourceAddress    string
	CollectedAddress string
	CurrentPrice     string
	FastAvailable    string
	FastTotal        string
	FastPower        string
	SuperAvailable   string
	SuperTotal       string
	SuperPower       string
	SlowAvailable    string
	SlowTotal        string
	SlowPower        string
	Payload          string
	CapturedAt       int64
	ReceivedAt       int64
	CollectionSource string
	PricePercentile  float64
	PricePeriodCount int
}

type payloadShape struct {
	StationName       string        `json:"stationName"`
	CurrentPrice      string        `json:"currentPrice"`
	BusinessHours     string        `json:"businessHours"`
	ParkingFee        string        `json:"parkingFee"`
	FastPrices        []PricePeriod `json:"fastPrices"`
	SlowPrices        []PricePeriod `json:"slowPrices"`
	ChargingPiles     []PileDetail  `json:"chargingPiles"`
	ChargingPileCount int           `json:"chargingPileCount"`
	ChargingPileTotal int           `json:"chargingPileTotal"`
}

func decodePayload(raw string) payloadShape {
	var payload payloadShape
	_ = json.Unmarshal([]byte(raw), &payload)
	return payload
}

func decodeMap(raw string) map[string]interface{} {
	result := map[string]interface{}{}
	_ = json.Unmarshal([]byte(raw), &result)
	return result
}
