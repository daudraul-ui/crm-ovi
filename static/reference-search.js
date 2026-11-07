(() => {
  for (const select of document.querySelectorAll('select[data-reference-kind]')) {
    const input = document.createElement('input');
    input.type = 'search';
    input.placeholder = 'Поиск в справочнике';
    input.setAttribute('aria-label', 'Поиск: ' + select.name);
    select.before(input);
    let timer, controller;
    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        if (controller) controller.abort();
        controller = new AbortController();
        const params = new URLSearchParams({q:input.value,field:select.name,context:select.form?.dataset.kind || '',chain:select.form?.elements.chain?.value || ''});
        try {
          const response = await fetch(`/references/${select.dataset.referenceKind}/?${params}`, {signal:controller.signal});
          if (!response.ok) throw Error('Поиск недоступен');
          const data = await response.json();
          const selected = [...select.selectedOptions].filter(o=>o.value).map(o=>new Option(o.text,o.value,true,true));
          select.replaceChildren(...(select.multiple?[]:[new Option('—','')]), ...selected);
          for (const row of data.items) if (!selected.some(o=>o.value===String(row.id))) select.add(new Option(row.name,row.id));
          input.setCustomValidity('');
        } catch (error) { if(error.name!=='AbortError') input.setCustomValidity('Не удалось выполнить поиск. Повторите запрос.'); }
      }, 250);
    });
  }
})();
