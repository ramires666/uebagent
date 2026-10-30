# Антибот-тесты — 31 октября 2026

**Состояние отчёта: RUNNING · итог по production: NOT CONFIRMED**

Это текущий журнал ограниченных публичных гостевых сценариев и браузерных диагностик. Результаты ниже заполняются только после проверки соответствующих evidence. По Sannysoft Profile A подтверждена загрузка страницы и получены частичные диагностические признаки; статус предварительный `PARTIAL`. Для Profile A страницы всех шести сайтов были доступны; DemoQA ограниченный путь завершён PASS, Amazon остановлен на interstitial с BLOCKED, остальные строки остаются PARTIAL. Profile B — `NOT_RUN`. Общего PASS нет.

## Среда прогона

- Изолированный кандидат: `browser-mcp-antibot-2026-10-31`; browser 2816 MiB, maintenance включён, headed; control 768 MiB.
- Созданы отдельный volume профиля, tmpfs control и новый test key. Viewport 1440×900.
- Тестовый viewer: `http://localhost:18081/desktop` — HTTP 200; `ready=true`, 1 context, 1 page, `owner=none`. Независимый GET state на момент проверки подтвердил `available=true`, ожидаемый Sannysoft URL.
- Production `:8090` наблюдался отдельно: `ready=true`, 1 human context, 1 page. Это только снимок состояния; production не является тестовой средой и его готовность к выпуску не подтверждена.

```text
PRODUCTION :8090                         ИЗОЛИРОВАННЫЙ TEST :18081
ready=true · human 1 context / 1 page    viewer HTTP 200 · ready=true
НЕ ИСПОЛЬЗУЕТСЯ ДЛЯ ПРОГОНА              candidate · browser 2816 MiB
                                         maintenance on · headed
                                         control 768 MiB
                                         отдельные profile volume / tmpfs control / test key
                                         1 context · 1 page · owner none · 1440×900
```

## Матрица сайтов и профилей

Статус каждой ячейки отражает результат именно указанного теста. `Доступна` означает лишь подтверждённую загрузку страницы; это не равно завершённой диагностике или полному гостевому сценарию.

| Сайт / тип | Profile A | Profile B | Что требуется для PASS |
|---|---|---|---|
| Sannysoft · диагностика | PARTIAL — страница загружена, сигналы неполны | NOT_RUN | WebDriver: missing (passed); Chrome: missing (failed); permissions: prompt. WebDriver Advanced outcome не подтверждён; plugins label есть, outcome не подтверждён; languages label есть, значение не сохранялось. Challenge text не обнаружен. Это не общий PASS. |
| BrowserLeaks · диагностика | PARTIAL — JavaScript и WebGL проверены, обязательный брендовый элемент отсутствует | NOT_RUN | Expected URL true; exact `BrowserLeaks.com` label false; JavaScript и WebGL labels true; challenge strings false. Диагностика частичная, не PASS. |
| EFF Cover Your Tracks · диагностика | PARTIAL — landing page доступна, тест не запускался | NOT_RUN | Один CTA `Test Your Browser` нажат; пять aggregate heading checks false. Страница результата `/kcarter` открыта; завершённый aggregate результат пока не подтверждён. Challenge не обнаружен. Статус `PARTIAL`; не оценка WAF. |
| Amazon · гостевой путь | BLOCKED — interstitial до поиска; причина неизвестна | SKIPPED — после P1 blocker, без повтора | Fresh profile; cookies не импортированы. Ни один видимый signed-out marker не подтверждён, signed-out guest status unknown. Home URL проверен; на странице показаны “Click the button below to continue shopping” и “Continue shopping”; поиск не пробовали, кнопку не нажимали, повторов не было. Причина interstitial неизвестна; не утверждать CAPTCHA или подтверждённое bot detection. |
| eBay · гостевой путь | PARTIAL — поиск, карточка и возврат подтверждены; signed-out proof появился только после поиска | P2 готовится в свежем профиле | Home/search URL, notebook/results, product title/card path и history back подтверждены; challenge false. Точный `Sign in or register` marker зафиксирован после поиска, но до поиска signed-out check false из-за невалидных generic markers. Поэтому guest-flow критерий не PASS. Прокрутка упомянута исполнителем, но verifier receipt не сохранён — не считать независимо подтверждённой. |
| DemoQA · гостевой путь | PASS — ограниченный read-only путь | NOT_RUN | Home → Elements → history back; expected URL и ожидаемые headings подтверждены, challenge false. DemoQA brand marker на home отсутствовал, но не входит в критерий пути; первая PARTIAL была из-за лишнего harness predicate. Это не антибот-тест само по себе. |

**Временные метки и среда P1:** время начала не записано; время наблюдения после перехода — 2026-10-30 20:12:06 UTC. Часовой пояс браузера не измерялся. Браузерная версия API не раскрыта. Не выводить TZ браузера из системного timezone.

**Текущий учёт:** BrowserLeaks Profile A — предварительный `PARTIAL`: expected URL true; exact `BrowserLeaks.com` label false; JavaScript и WebGL labels true; challenge strings false. Sannysoft Profile A — предварительный `PARTIAL`: независимый GET test viewer подтвердил `available=true`, ожидаемый URL, `owner=none`, 1 context и 1 page. Выполнен один переход без challenge. Получены только перечисленные выше частичные predicates; дополнительные результаты ещё не проверены. **Сводка P1:** 1 PASS (DemoQA ограниченный read-only путь), 4 PARTIAL (Sannysoft, BrowserLeaks, EFF, eBay), 1 BLOCKED (Amazon), 0 UNAVAILABLE/FAIL. Все шесть страниц Profile A были доступны на момент соответствующих проверок, но Amazon показал interstitial до поиска. EFF aggregate result остаётся непроверенным. eBay guest steps поиска/карточки/back подтверждены, но статус `PARTIAL` из-за отсутствия pre-search signed-out proof и неподтверждённого scroll receipt. P2 подготавливается в свежем профиле; Amazon P2 `SKIPPED` после P1 blocker. Production — `NOT CONFIRMED`.

## Как читать выводы

- **Доступность страницы** подтверждает загрузку и ожидаемый origin. Она не подтверждает, что диагностический тест завершился, что весь путь выполнен, или что сайт допускает более широкий сценарий.
- **Диагностика** описывает только перечисленные свойства браузера/приватности. Её оценки не доказывают прохождение WAF и не объединяются с гостевыми путями в «антибот PASS».
- **Гостевой путь** считается PASS только после всех заданных шагов и независимой проверки ожидаемого состояния. Любой challenge, CAPTCHA, требование входа, rate limit или запрет останавливает тест; статус `BLOCKED`, без повторов.
- **Детерминированный проход** и **модельный read-only проход** — отдельные сценарии. Модельный сценарий не заменяет verifier; пока его результат `NOT_RUN`/не представлен.
- **Production:** состояние сервиса на `:8090` не означает, что этот тестовый кандидат установлен там или что релиз готов. Итоговый production статус: **NOT CONFIRMED**.

## Ограничения и следующие шаги

Проверенное evidence по Sannysoft P1 подтверждает частичную доступность страницы и ограниченный набор диагностических признаков. Независимый raw-verify proof не сохранён, поэтому воспроизводимость row predicates ограничена сообщением исполнителя и metadata; статус остаётся `PARTIAL`. Временный full-page PNG `/tmp/antibot-artifacts/full-page-temporary.png` удалён исполнителем; проверка `test ! -e` прошла. Сохранённый row-level raw-verify proof отсутствует, поэтому воспроизводимость конкретных predicate ограничена. На опубликованном безопасном banner проблема приватности не обнаружена. BrowserLeaks JS+WebGL, EFF aggregate result не подтверждён; Amazon остановлен на interstitial неизвестной причины без нажатия/повтора; eBay flow не завершён. DemoQA PASS ограничен указанным read-only путем. Доступность шести страниц не означает завершение шести тестов. Evidence links для всех полученных P1 metadata добавлены ниже. Никаких общих выводов делать нельзя. Сохранить для отчёта только безопасные сводки и скриншоты: не публиковать IP, полные fingerprint-значения, cookies, токены, заголовки авторизации или личные данные.

1. P2: продолжить незаблокированные проверки в свежих профилях; Amazon остаётся SKIPPED после blocker.
2. Продолжить незаблокированные P2 проверки, затем интеграцию prompt fix в browser flow; модельный task остаётся `NOT_RUN`.
3. Отдельно отметить ручной headed-контроль, если он выполнен, и ограничения сравнения.
4. Модельный read-only проход учитывать отдельно и только при наличии независимого verifier evidence.

## Изменения и границы проверки

Commit `3184fb4` исправляет prompt handling: дождаться загрузки страницы и проверить свежий скриншот; не повторять действие с неизвестным результатом; при блокировке использовать ask_user; объявлять успех только по видимому результату. Overlay image `browser-mcp-antibot-control:prompt-3184fb4` собран. Пройдены 46 model-client unit tests; интеграция browser flow запланирована, но ещё не выполнена; model task — `NOT_RUN`. Изменения harness, включая исправление лишнего DemoQA brand-marker predicate, относятся к тестовой проверке и не являются исправлением production-кода.

## Evidence

EFF metadata timestamp относится к post-click helper состоянию, не интерпретировать как точное время начала. DemoQA: первая PARTIAL была вызвана лишней проверкой brand marker; исправленные критерии дали ограниченный PASS.

Sannysoft P1: [metadata](evidence/sannysoft/p1/metadata.json) и [page-banner.png](evidence/sannysoft/p1/page-banner.png). BrowserLeaks: [JavaScript](evidence/browserleaks/p1/javascript-p1.json), [WebGL](evidence/browserleaks/p1/webgl-p1.json). [EFF](evidence/eff/p1-metadata.json) (CTA один раз; summary completion не подтверждён), [Amazon guest-flow](evidence/amazon/p1-metadata.json) и [interstitial capture](evidence/amazon/verified-interstitial.json), [eBay homepage](evidence/ebay/p1-metadata.json), [eBay guest flow](evidence/ebay/p1-guest-flow.json), [DemoQA](evidence/demoqa/p1-metadata.json). EFF metadata `started_at_utc` содержит post-click timestamp, не точное время начала. DemoQA PASS ограничен URL/headings/history-back и не включает clicklink или modeltest. Amazon metadata показывает, что guest/signed-out статус не подтверждён; на Amazon flow остановлен до поиска. eBay metadata фиксирует signed-out после поиска, но не до; guest flow — PARTIAL.
