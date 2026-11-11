(() => {
  const form = document.querySelector('[data-store-form]');
  if (!form) return;
  const chain = form.elements.chain;
  const company = form.elements.company;
  const format = form.elements.format;
  const error = document.querySelector('#store-options-error');
  let pending = null;
  const replaceOptions = (select, rows) => {
    if (!select) return;
    select.replaceChildren(new Option('---------', ''));
    rows.forEach(row => select.add(new Option(row.name, row.id)));
  };
  chain.addEventListener('change', async () => {
    if (pending) pending.abort();
    pending = new AbortController();
    const selected = chain.value;
    const hadSelection = Boolean(company?.value || format.value);
    replaceOptions(company, []);
    replaceOptions(format, []);
    error.textContent = '';
    try {
      const response = await fetch(`${form.dataset.optionsUrl}?chain=${encodeURIComponent(selected)}`, {
        signal: pending.signal, headers: {'Accept': 'application/json'}
      });
      if (!response.ok) throw new Error('Не удалось загрузить юридические лица и форматы. Повторите выбор сети.');
      const data = await response.json();
      if (chain.value !== selected) return;
      replaceOptions(company, data.companies);
      replaceOptions(format, data.formats);
      if (hadSelection) error.textContent = 'Сеть изменена: проверьте клиента и формат магазина. ';
      if (company && !data.companies.length && selected) {
        error.append(document.createTextNode('У сети нет связанных клиентов. '));
        const link = document.createElement('a');
        link.href = `/chains/${encodeURIComponent(selected)}/edit/`;
        link.textContent = 'Изменить торговую сеть';
        error.append(link);
      }
    } catch (e) {
      if (e.name !== 'AbortError') error.textContent = e.message;
    }
  });
  const dialog = document.querySelector('#format-dialog');
  if (!dialog) return;
  const name = document.querySelector('#format-name');
  const message = document.querySelector('#format-error');
  const save = document.querySelector('#save-format');
  document.querySelector('#add-format').addEventListener('click', () => {
    if (!chain.value) { error.textContent = 'Сначала выберите торговую сеть.'; chain.focus(); return; }
    message.textContent = '';
    name.value = '';
    dialog.showModal();
    name.focus();
  });
  document.querySelector('#cancel-format').addEventListener('click', () => dialog.close());
  name.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); save.click(); }
  });
  save.addEventListener('click', async () => {
    if (save.disabled) return;
    if (!name.value.trim()) { message.textContent = 'Введите название формата.'; name.focus(); return; }
    save.disabled = true;
    message.textContent = '';
    const selected = chain.value;
    try {
      const response = await fetch(form.dataset.formatUrl, {
        method: 'POST', headers: {'X-CSRFToken': form.elements.csrfmiddlewaretoken.value, 'Accept': 'application/json'},
        body: new URLSearchParams({name: name.value.trim(), chain: selected})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.errors ? Object.values(data.errors).flat().join(' ') : 'Не удалось создать формат.');
      if (chain.value === selected) { format.add(new Option(data.name, data.id)); format.value = String(data.id); }
      dialog.close();
      format.focus();
    } catch (e) { message.textContent = e.message; }
    finally { save.disabled = false; }
  });
})();
