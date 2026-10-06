# Payment Operations Analytics Simulator

Локальна пісочниця для **аналітика платіжних операцій**. Вона запускає невелику платіжну платформу: генератор трафіку мерчантів, платіжний шлюз з маршрутизацією та каскадуванням, чотири симульовані PSP, PostgreSQL, детектор аномалій і дашборд центру керування. У неї можна вносити інциденти, спостерігати, як вони проявляються в метриках, і виявляти їх за допомогою SQL, Python або BI.

```
 traffic :8002 ──POST /v1/payments──▶ gateway :8000 ──/{provider}/authorize──▶ providers :8001
 (merchant simulator,                 (weighted routing,                      AlphaPay · BetaGate ·
  refunds, volume)                     cascade, smart routing)                 GammaPSP · DeltaAcq
        │                                   │ INSERT every attempt                  ▲
        └────── settings / chaos rules ──▶ PostgreSQL :5432 ◀──── chaos_rules ───────┘
                                            ▲          ▲
                    detector :8003 ─────────┘          └──── dashboard :8080 (UI + API)
                    (live z-tests + history scan)            Tableau / Metabase / Adminer
```

## Швидкий старт

```bash
python opa.py up          # запускає все й відкриває http://localhost:8080
python opa.py seed        # 500 тис. історичних транзакцій за 30 днів, 6 внесених аномалій (~35 с)
python opa.py traffic start 25
python opa.py demo        # інцидент: AlphaPay +30 п.п. відмов -> виявлення -> smart routing -> відновлення
```

`opa.py` використовує лише стандартну бібліотеку й автоматично обирає режим запуску:

| Режим | Коли | Що запускається |
|---|---|---|
| **Docker** | запущено Docker Desktop | `docker compose` з Postgres 16, 5 контейнерами застосунку, Adminer, опційно Metabase (`--bi`) і гарячим перезавантаженням `./app` |
| **Local** | Docker немає (або `--local`) | портативний PostgreSQL 16 (завантажується один раз, без прав адміністратора) плюс 5 Python-процесів у `.venv` |

У локальному режимі база даних зберігається в `%LOCALAPPDATA%\opanalytics` (або `~/.local/share/opanalytics`), а не поруч із кодом, бо запис на USB-флешку приблизно в 50 разів повільніший. Розташування можна змінити змінною `OPA_DATA_DIR`.

## Що можна робити

| Де | Що |
|---|---|
| **Overview** | Частка схвалень / конверсія (після каскаду) / частка відмов / помилок / повернень, обсяг, середня та p95 затримка, зміна порівняно з попереднім вікном, часові ряди по провайдерах, структура причин відмов, відкриті аномалії |
| **Breakdown** | Будь-який KPI у розрізі провайдера / країни / мерчанта / методу / валюти / причини відмови; теплова карта схвалень «країна × провайдер» |
| **Anomalies** | Інциденти в реальному часі та історичні, з рівнем серйозності, z-оцінкою та групуванням за першопричиною. *Investigate* відкриває SQL Lab з готовим запитом. Recall/precision рахуються відносно внесеної еталонної розмітки |
| **Live feed** | Кожна спроба в момент її виконання; каскадні повтори позначено `#2` |
| **Traffic & routing** | Старт/стоп, TPS, сплески; каскадування, smart routing, тайм-аут; для кожного провайдера: увімкнення / вага / базова частка схвалень / затримка; вікно та чутливість детектора |
| **Chaos lab** | 7 готових сценаріїв інцидентів плюс власні правила: `decline_rate`, `latency_ms`, `error_rate`, `refund_rate`, `volume_mult` для будь-якого провайдера / країни / мерчанта / методу, з автоматичним завершенням |
| **Payment tester** | Надіслати один або N платежів через справжній шлюз (вибір мерчанта, країни, методу або примусового провайдера), а потім зробити повернення |
| **Dataset & export** | Генерація датасету будь-якого розміру, експорт CSV для Tableau/Excel, скидання |
| **SQL lab** | SQL лише для читання по всій БД, із запитами з `sql/` як прикладами |
| **Services** | Стан кожного сервісу, посилання на Swagger `/docs` кожного з них, інформація про БД |

Кожен сервіс має Swagger: шлюз http://localhost:8000/docs, PSP :8001/docs, трафік :8002/docs, детектор :8003/docs, API дашборду :8080/docs.

## CLI

```
python opa.py status | logs [svc] [-f] | restart [svc] | down [-v]
python opa.py seed --rows 1000000 --days 60
python opa.py traffic start 40 | stop | burst 2000 | stats
python opa.py chaos presets | inject provider_latency --minutes 10 | list | clear
python opa.py demo --preset country_low_success      # будь-який сценарій
python opa.py detect [--history]   |  python opa.py anomalies --status all
python opa.py pay --merchant m_017 --country MX --amount 30 -n 20
python opa.py sql "SELECT provider, count(*) FROM transactions GROUP BY 1"
python opa.py test                 # юніт-тести (БД не потрібна)
python opa.py psql                 # SQL-консоль
```

## Модель даних

У `transactions` один рядок відповідає одній **спробі**. Платіж, який було каскадовано на другого провайдера, має два рядки з однаковим `payment_id`.

`transaction_id, payment_id, attempt_no, merchant_id, created_at, country, currency, amount, amount_usd, payment_method, provider, status (approved|declined|error), decline_reason, processing_time_ms, refunded, refunded_at, source (seed|live)`

Інші таблиці: `merchants`, `providers`, `chaos_rules`, `settings`, `anomalies`, `seed_ground_truth`.
BI-представлення: `v_transactions`, `v_hourly_provider`, `v_daily_kpis`, `v_daily_merchant`, `v_country_provider`.

## Внесені аномалії (історичні дані)

| # | Аномалія | Зріз | Метрика |
|---|---|---|---|
| 1 | Провайдер A раптово відхиляє на 30 п.п. більше протягом 6 год | provider=alphapay | approval_rate |
| 2 | Збій інтеграції мерчанта, трафік на рівні 8% протягом 12 год | merchant=m_009 | volume_share |
| 3 | Затримка провайдера B +900 мс протягом 36 год | provider=betagate | latency |
| 4 | Хвиля повернень у мерчанта X +18 п.п. протягом 3 днів | merchant=m_017 | refund_rate |
| 5 | Емітенти країни Y відхиляють як suspected_fraud протягом 20 год | country=BR | approval_rate |
| 6 | Тайм-аути / 5xx у провайдера C на рівні 15% протягом 3 год | provider=gammapsp | error_rate |

Їх можна знайти за допомогою SQL у [`sql/anomalies/`](sql/anomalies), Python-детектора (*Scan history*) і в Tableau. На стандартних даних детектор досягає **recall 100%** і **precision ≈ 86%**.

## Як працює виявлення

* **У реальному часі** (кожні 20 с): останні 5 хв живого трафіку порівнюються з 60-хвилинною базою, яка закінчується на 5 хв раніше. Цей проміжок не дає поточному інциденту потрапити у власну базу порівняння. Використовувані перевірки:
  * z-тест для двох пропорцій для частки схвалень і помилок;
  * z-статистика Велча плюс відношення для затримки;
  * відношення частот для повернень;
  * пуассонівська z-оцінка частки трафіку для обсягу, яка залишається стійкою при зміні TPS.

  Інциденти відкриваються, оновлюються й автоматично закриваються.
* **Історія**: погодинні інтервали кожного зрізу порівнюються з робастною базою цього зрізу (медіана/MAD). Послідовні позначені години об'єднуються в один інцидент.
* **Групування за першопричиною**: якщо зріз пояснюється ширшим одночасним інцидентом, він позначається як *"likely explained by provider=betagate"* замість того, щоб показуватися окремим інцидентом. Наприклад, під час стрибка затримки BetaGate затримка зростає й у всіх мерчантів.

## BI-інструменти: Tableau, Power BI, Excel

Відкрийте **BI tools** у дашборді (http://localhost:8080/#bi). Там є:

* параметри підключення для користувача лише для читання `analyst` / `analyst` і кнопка перевірки доступу;
* **Tableau**: `.tds`-джерело даних для кожного датасету (живий PostgreSQL у Tableau Desktop) і екстракт `payops.hyper`
  з усіма датасетами, який відкривається в безкоштовному Tableau Public;
* **Power BI**: файли підключення `.pbids` (PostgreSQL Import / DirectQuery або CSV через HTTP),
  готовий код Power Query (M) і DAX-міри;
* **OData v4 фід** за адресою http://localhost:8080/odata, який працює в Power BI, Excel і Tableau без
  жодного драйвера БД;
* CSV-посилання для кожного датасету.

Для екстракту `.hyper` потрібен опційний Tableau Hyper API: `.venv\Scripts\pip install -r requirements-bi.txt`.

Навчальний курс російською мовою з вправами на пошук внесених аномалій: [bi/GUIDE_RU.md](bi/GUIDE_RU.md).
Обчислювані поля для Tableau: [tableau/README.md](tableau/README.md).

## Структура проєкту

```
opa.py                     CLI: запуск / керування / тести
docker-compose.yml, Dockerfile
app/opanalytics/
  reference.py             мерчанти, провайдери, країни, модель схвалень і затримок
  chaos.py                 правила інцидентів + готові сценарії
  routing.py               зважена маршрутизація, smart routing (ковзний стан здоров'я)
  detection.py             статистика, детектори реального часу й пакетні, групування за першопричиною, оцінювання
  seed.py                  векторизований генератор на 500 тис. + COPY
  services/                gateway, providers, traffic, detector, dashboard (FastAPI)
  static/                  UI дашборду (vanilla JS + Chart.js)
sql/anomalies, sql/kpi     запити аналітика
tests/                     pytest
```
