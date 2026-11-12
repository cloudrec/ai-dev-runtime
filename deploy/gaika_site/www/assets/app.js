(function () {
  'use strict';
  const translations = window.GAIKA_TRANSLATIONS || {};
  const SUPPORTED = ['ru', 'uk', 'en', 'pl', 'de', 'fr', 'ro', 'cs', 'hu', 'lt', 'lv', 'et', 'nl', 'es', 'it', 'pt', 'sk', 'bg', 'da'];
  const langs = new Set(SUPPORTED.filter((code) => code === 'ru' || translations[code]));
  const elements = [...document.querySelectorAll('[data-i18n], [data-i18n-html], [data-i18n-placeholder]')];
  const originals = new Map(elements.map(el => [el, {
    value: el.hasAttribute('data-i18n-html') ? el.innerHTML : el.hasAttribute('data-i18n-placeholder') ? el.getAttribute('placeholder') : el.textContent,
    kind: el.hasAttribute('data-i18n-html') ? 'html' : el.hasAttribute('data-i18n-placeholder') ? 'placeholder' : 'text',
    key: el.getAttribute('data-i18n-html') || el.getAttribute('data-i18n-placeholder') || el.getAttribute('data-i18n')
  }]));
  const stringsRU = {
    cheapName: 'Экономная корзина', balanceName: 'Оптимальная корзина', comfortName: 'Комфортная корзина',
    subApproved: 'Одобрено только в демо. Корзина магазина не изменена.', subRejected: 'Отклонено только в демо. Корзина магазина не изменена.',
    statusCart: 'собирает корзину', statusTesting: 'корзина готова — нужны тестеры', statusCatalog: 'находит и считает цены', statusComing: 'скоро',
    pageTitleHome: 'GAIKA — покупки по вашему списку', pageTitleBiz: 'GAIKA — для ретейлеров'
  };
  const fallbackTitles = {
    en: ['GAIKA — from list to basket', 'GAIKA — for retailers'], uk: ['GAIKA — покупки за вашим списком', 'GAIKA — для ритейлерів'],
    de: ['GAIKA — vom Einkaufszettel zum Warenkorb', 'GAIKA — für Händler'], pl: ['GAIKA — od listy do koszyka', 'GAIKA — dla sieci handlowych'],
    fr: ['GAIKA — de la liste au panier', 'GAIKA — pour les enseignes']
  };
  let lang = 'ru';
  function tr(key) { return (lang === 'ru' ? stringsRU[key] : translations[lang]?.[key]) || translations.en?.[key] || stringsRU[key] || ''; }

  const STATUS_KEY = { cart: 'statusCart', testing: 'statusTesting', catalog: 'statusCatalog', coming: 'statusComing' };
  function renderMarkets() {
    const data = window.GAIKA_MARKETS;
    const grid = document.getElementById('market-grid');
    if (!data || !grid) return;
    let names = null;
    try { names = new Intl.DisplayNames([lang], { type: 'region' }); } catch (e) {}
    const countryName = (cc) => { try { return names?.of(cc.toUpperCase()) || cc.toUpperCase(); } catch (e) { return cc.toUpperCase(); } };
    const order = ['cart', 'testing', 'catalog', 'coming'];
    const live = data.markets.filter(m => m.shops.some(([, st]) => st !== 'coming'));
    const liveShops = live.flatMap(m => m.shops.filter(([, st]) => st !== 'coming'));
    const ready = liveShops.filter(([, st]) => st === 'cart' || st === 'testing');
    const nf = new Intl.NumberFormat(lang);
    const set = (id, n) => { const el = document.getElementById(id); if (el) el.textContent = nf.format(n); };
    set('stat-countries', live.length); set('stat-shops', liveShops.length); set('stat-ready', ready.length);
    grid.textContent = '';
    for (const market of data.markets) {
      const best = Math.min(...market.shops.map(([, st]) => order.indexOf(st)));
      const card = document.createElement('section');
      card.className = 'market-card is-' + (order[best] || 'coming');
      const h = document.createElement('h3');
      const cc = document.createElement('span'); cc.className = 'cc'; cc.textContent = market.cc.toUpperCase();
      h.append(cc, document.createTextNode(countryName(market.cc)));
      const ul = document.createElement('ul'); ul.className = 'shop-chips';
      for (const [name, status] of market.shops) {
        const li = document.createElement('li'); li.className = 'chip is-' + status; li.textContent = name; li.title = tr(STATUS_KEY[status]);
        ul.append(li);
      }
      card.append(h, ul);
      grid.append(card);
    }
    const needed = document.getElementById('testers-needed');
    if (needed) {
      needed.textContent = '';
      const wanted = data.markets.filter(m => m.shops.some(([, st]) => st === 'testing'))
        .map(m => ({ cc: m.cc, name: countryName(m.cc) }))
        .sort((a, b) => a.name.localeCompare(b.name, lang));
      for (const m of wanted) {
        const li = document.createElement('li');
        const cc = document.createElement('span'); cc.className = 'cc'; cc.textContent = m.cc.toUpperCase();
        li.append(cc, document.createTextNode(m.name));
        needed.append(li);
      }
    }
  }

  function setLanguage(code) {
    lang = langs.has(code) ? code : 'ru';
    document.documentElement.lang = lang;
    for (const el of elements) {
      const orig = originals.get(el), next = lang === 'ru' ? orig.value : (translations[lang]?.[orig.key] ?? translations.en?.[orig.key]);
      if (next === undefined || next === null) continue;
      if (orig.kind === 'html') el.innerHTML = next;
      else if (orig.kind === 'placeholder') el.setAttribute('placeholder', next);
      else el.textContent = next;
    }
    const biz = document.body.dataset.page === 'retailers';
    const own = tr(biz ? 'pageTitleBiz' : 'pageTitleHome');
    document.title = (lang !== 'ru' && !translations[lang]?.[biz ? 'pageTitleBiz' : 'pageTitleHome'] && fallbackTitles[lang]?.[biz ? 1 : 0]) || own;
    const switcher = document.getElementById('lang-switch'); if (switcher) switcher.value = lang;
    try { localStorage.setItem('gaika-preferred-language', lang); } catch (e) {}
    if (location.protocol === 'http:' || location.protocol === 'https:') { const url = new URL(location.href); url.searchParams.set('lang', lang); history.replaceState(null, '', url.pathname + url.search + url.hash); }
    updatePlan();
    renderMarkets();
  }
  function getInitialLanguage() {
    const param = new URLSearchParams(location.search).get('lang'); if (langs.has(param)) return param;
    try { const saved = localStorage.getItem('gaika-preferred-language'); if (langs.has(saved)) return saved; } catch (e) {}
    for (const tag of (navigator.languages || [navigator.language || 'ru'])) { const primary = String(tag).toLowerCase().split('-')[0]; if (langs.has(primary)) return primary; }
    return 'en';
  }
  const switcher = document.getElementById('lang-switch');
  if (switcher) {
    for (const opt of [...switcher.options]) if (!langs.has(opt.value)) opt.remove();
    switcher.addEventListener('change', e => setLanguage(e.target.value));
  }
  const plans = { cheap: { price: '≈ €24,80', name: 'cheapName' }, balance: { price: '≈ €27,40', name: 'balanceName' }, comfort: { price: '≈ €30,10', name: 'comfortName' } };
  let selectedPlan = 'balance';
  function updatePlan() { const price = document.getElementById('plan-price'); if (!price) return; price.textContent = plans[selectedPlan].price; document.getElementById('total-price').textContent = plans[selectedPlan].price; document.getElementById('plan-title').textContent = tr(plans[selectedPlan].name); }
  for (const btn of document.querySelectorAll('[data-plan]')) btn.addEventListener('click', () => { selectedPlan = btn.dataset.plan; for (const el of document.querySelectorAll('[data-plan]')) { el.classList.toggle('active', el === btn); el.setAttribute('aria-pressed', String(el === btn)); } updatePlan(); });
  const feedback = document.getElementById('sub-feedback'); const approve = document.getElementById('approve-sub'), reject = document.getElementById('reject-sub');
  if (approve && reject && feedback) { for (const btn of [approve, reject]) btn.addEventListener('click', () => { approve.setAttribute('aria-pressed', String(btn === approve)); reject.setAttribute('aria-pressed', String(btn === reject)); feedback.textContent = tr(btn === approve ? 'subApproved' : 'subRejected'); }); }
  document.querySelectorAll('.mobile-nav-pop a').forEach(a => a.addEventListener('click', () => { a.closest('details').open = false; }));
  setLanguage(getInitialLanguage());
})();
