(function () {
  function updateToggle(button, collapsed) {
    button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    button.textContent = collapsed ? 'Show' : 'Hide';
  }

  document.querySelectorAll('[data-set-toggle]').forEach((button) => {
    const block = button.closest('.set-block');
    if (!block) return;
    updateToggle(button, block.classList.contains('collapsed'));

    button.addEventListener('click', () => {
      const collapsed = block.classList.toggle('collapsed');
      updateToggle(button, collapsed);
    });
  });

  const anchored = window.location.hash ? window.location.hash.slice(1) : '';
  if (anchored) {
    const el = document.getElementById(anchored);
    if (el) {
      const block = el.closest('.set-block');
      if (block) {
        block.classList.remove('collapsed');
        const button = block.querySelector('[data-set-toggle]');
        if (button) updateToggle(button, false);
      }
      el.classList.add('just-updated');
      setTimeout(() => el.classList.remove('just-updated'), 1200);
    }
  }

  const lookupBtn = document.getElementById('lookup-btn');
  if (lookupBtn) {
    const setCodeInput = document.getElementById('set_code');
    const collectorInput = document.getElementById('collector_number');
    const finishSelect = document.getElementById('finish');
    const statusEl = document.getElementById('lookup-status');
    const preview = document.getElementById('lookup-preview');
    const previewLink = document.getElementById('lookup-preview-link');
    const previewImg = document.getElementById('lookup-preview-img');
    const previewName = document.getElementById('lookup-preview-name');
    const previewSet = document.getElementById('lookup-preview-set');
    const previewId = document.getElementById('lookup-preview-id');

    function setStatus(message, kind) {
      statusEl.textContent = message || '';
      statusEl.className = 'lookup-status' + (kind ? ' ' + kind : '');
    }

    function resetPreview() {
      preview.hidden = true;
      setStatus('', '');
    }

    function restrictFinishes(finishes) {
      const previousValue = finishSelect.value;
      finishSelect.innerHTML = '';
      finishes.forEach((finish) => {
        const option = document.createElement('option');
        option.value = finish;
        option.textContent = finish.charAt(0).toUpperCase() + finish.slice(1);
        finishSelect.appendChild(option);
      });
      if (finishes.includes(previousValue)) {
        finishSelect.value = previousValue;
      }
    }

    [setCodeInput, collectorInput].forEach((input) => {
      input.addEventListener('input', resetPreview);
    });

    lookupBtn.addEventListener('click', async () => {
      const setCode = setCodeInput.value.trim();
      const collectorNumber = collectorInput.value.trim();
      if (!setCode || !collectorNumber) {
        setStatus('Enter a set code and collector number first.', 'error');
        return;
      }

      setStatus('Checking Scryfall…', '');
      preview.hidden = true;
      lookupBtn.disabled = true;

      try {
        const params = new URLSearchParams({ set: setCode, cn: collectorNumber });
        const response = await fetch(`/api/card-lookup?${params.toString()}`);
        const data = await response.json();

        if (!response.ok || !data.ok) {
          setStatus(data.error || 'Could not find that card.', 'error');
          preview.hidden = true;
          return;
        }

        previewImg.src = data.image_small || '';
        previewImg.alt = data.name || 'Card preview';
        previewLink.href = data.scryfall_uri || '#';
        previewName.textContent = data.name || '';
        previewSet.textContent = `${data.set_name || ''} (${(data.set_code || '').toUpperCase()}) #${data.collector_number || ''}`;
        previewId.textContent = data.scryfall_id || '';
        restrictFinishes(data.finishes && data.finishes.length ? data.finishes : ['nonfoil', 'foil', 'etched']);
        preview.hidden = false;
        setStatus('Card found — confirm this looks right, then add it.', 'success');
      } catch (err) {
        setStatus('Lookup failed. Check your connection and try again.', 'error');
      } finally {
        lookupBtn.disabled = false;
      }
    });
  }
})();
