import json
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime
import daily_tariff_policy as policy

NOW = int(datetime.fromisoformat('2026-11-11T12:00:00+08:00').timestamp())
def period(time='00:00-24:00', total='0.8', fee='0.2'):
    return dict(time=time, totalPrice=total, elecFee='0.6', serviceFee=fee)
def observation(captured=NOW-3600, **changes):
    price = dict(priceSchedulePlatform='BAIDU', priceScheduleCapturedAt=captured*1000, fastPrices=[period()])
    price.update(changes)
    return dict(captured_at=NOW, price_json=price)

class DailyTariffTests(unittest.TestCase):
    def test_complete_zero_midnight_and_auxiliary_rows(self):
        self.assertTrue(policy.complete_periods([period('22:00-08:00'), period('08:00-22:00')]))
        self.assertTrue(policy.complete_periods([period(),period('当前时段')]))
        self.assertTrue(policy.complete_periods([period('00：00—23：59','0.6','0')]))
        self.assertFalse(policy.complete_periods([period('当前时段')]))
    def test_incomplete_or_conflicting_plan_is_not_reused(self):
        for rows in ([period('00:00-12:00')], [period(fee='')], [period(fee='0.9')], [period(),period('08:00-09:00')]):
            self.assertIsNone(policy.cached_tariff([observation(fastPrices=rows)],'BAIDU',NOW))
    def test_cache_keeps_original_time_and_respects_platform(self):
        result=policy.cached_tariff([observation()],'BAIDU',NOW)
        self.assertEqual(result['capturedAt'], (NOW-3600)*1000)
        self.assertIsNone(policy.cached_tariff([observation()],'AMAP',NOW))
    def test_previous_day_six_hour_expiry_and_future_origin_force_full_read(self):
        for timestamp in (NOW-21600, NOW+1, int(datetime.fromisoformat('2026-11-10T23:59:00+08:00').timestamp())):
            self.assertIsNone(policy.cached_tariff([observation(timestamp)],'BAIDU',NOW))
    def test_latest_incomplete_plan_does_not_resurrect_older_complete_plan(self):
        self.assertIsNone(policy.cached_tariff([observation(fastPrices=[]),observation()],'BAIDU',NOW))
    def test_json_string_can_be_read_without_assuming_business_source_is_map(self):
        row=observation(); row['price_json']=json.dumps(row['price_json'])
        self.assertIsNotNone(policy.cached_tariff([row],'BAIDU',NOW))
        self.assertIsNone(policy.cached_tariff([observation(priceSchedulePlatform='HENAN_POI')],'BAIDU',NOW))

class ClaimTariffTests(unittest.TestCase):
    def test_enrichment_reads_only_the_claimed_station_and_never_blocks_claim_on_error(self):
        import api_server
        task=dict(type='HENAN_POI_DETAIL',stationId='B123',stationName='Station')
        conn=MagicMock(); cursor=conn.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value=[observation()]
        with patch.object(api_server,'_henan_mysql_conn',return_value=conn),patch.object(api_server.time,'time',return_value=NOW):
            result=api_server._with_daily_tariff(task,'BAIDU')
        self.assertIsNotNone(result['dailyTariff'])
        sql,args=cursor.execute.call_args.args
        self.assertIn('source_key=%s',sql)
        self.assertIn('ORDER BY captured_at DESC, id DESC LIMIT 1',sql)
        self.assertEqual(args,(api_server.station_source_key('B123'),NOW-86400,'BAIDU'))
        conn.close.assert_called_once()
        with patch.object(api_server,'_henan_mysql_conn',side_effect=RuntimeError('unavailable')):
            self.assertIs(api_server._with_daily_tariff(task,'BAIDU'),task)

if __name__=='__main__':
    unittest.main()
