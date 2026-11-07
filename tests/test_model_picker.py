"""The browser must select an exact picker option before submitting."""
import asyncio

import pytest

from arena_api._direct_page import _select_model_in_picker
from arena_api.exceptions import ModelUnavailableError


class Picker:
    def __init__(self, *, disabled=False, missing=False, confirm=True):
        self.disabled, self.missing, self.confirm = disabled, missing, confirm
        self.events = []
        self.selected = 'Max'

    def locator(self, selector):
        self.events.append(('locate', selector))
        return Node(self, 'dialog' if '[role="dialog"]' in selector else 'trigger')


class Node:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    @property
    def first(self):
        return self

    def locator(self, selector):
        self.page.events.append(('locate', selector))
        return Node(self.page, 'name' if 'font-mono' in selector else 'option')

    async def wait_for(self, *, state, timeout):
        self.page.events.append(('wait', self.kind, state))
        if self.kind == 'option' and self.page.missing:
            raise TimeoutError('missing')

    async def click(self, *, timeout):
        self.page.events.append(('click', self.kind))
        if self.kind == 'option' and self.page.confirm:
            self.page.selected = 'gemini-test'

    async def get_attribute(self, name):
        return 'true' if self.page.disabled and name == 'aria-disabled' else 'false'

    async def text_content(self):
        return 'gemini-test' if self.kind == 'name' else self.page.selected

    async def scroll_into_view_if_needed(self, *, timeout):
        self.page.events.append(('scroll', self.kind))


def test_selects_exact_id_and_scrolls_before_click():
    page = Picker()
    asyncio.run(_select_model_in_picker(page, 'model-123'))
    assert ('locate', '[role="option"][data-value="model-123"]') in page.events
    assert page.events.index(('scroll', 'option')) < page.events.index(('click', 'option'))
    assert ('wait', 'dialog', 'hidden') in page.events


def test_max_uses_its_exact_name():
    page = Picker()
    asyncio.run(_select_model_in_picker(page))
    assert ('locate', '[role="option"]:has(span:text-is("Max"))') in page.events


@pytest.mark.parametrize('kwargs', [{'missing': True}, {'disabled': True}])
def test_missing_or_disabled_model_fails_before_click(kwargs):
    page = Picker(**kwargs)
    with pytest.raises(ModelUnavailableError):
        asyncio.run(_select_model_in_picker(page, 'model-123'))
    assert ('click', 'option') not in page.events


def test_unconfirmed_selection_fails(monkeypatch):
    async def no_wait(_):
        pass
    monkeypatch.setattr('arena_api._direct_page.asyncio.sleep', no_wait)
    with pytest.raises(ModelUnavailableError, match='did not confirm'):
        asyncio.run(_select_model_in_picker(Picker(confirm=False), 'model-123'))


@pytest.mark.parametrize('category,label', [
    ('text', 'Text'), ('code', 'Code'), ('image', 'Image'), ('search', 'Search'),
])
def test_category_is_clicked_before_model(category, label):
    class FilterNode(Node):
        async def get_attribute(self, name):
            if name == 'aria-pressed':
                return 'true' if ('click', 'filter') in self.page.events else 'false'
            return None

    class FilterDialog(Node):
        def locator(self, selector):
            if '[data-arena-buttons]' in selector:
                self.page.events.append(('locate', selector))
                return FilterNode(self.page, 'filter')
            return super().locator(selector)

    class FilterPicker(Picker):
        def locator(self, selector):
            self.events.append(('locate', selector))
            return FilterDialog(self, 'dialog') if '[role="dialog"]' in selector else Node(self, 'trigger')

    page = FilterPicker()
    asyncio.run(_select_model_in_picker(page, 'model-123', category=category))
    assert ('locate', f'[data-arena-buttons] button[aria-label="{label}"]') in page.events
    assert page.events.index(('click', 'filter')) < page.events.index(('scroll', 'option'))


def test_bad_category_rejected():
    from arena_api.auto import AutoOptions
    with pytest.raises(ValueError, match='model_picker_category'):
        AutoOptions(model_picker_category='video')


def test_active_filter_is_not_toggled_off():
    class ActiveFilter(Node):
        async def get_attribute(self, name):
            if name == 'class':
                return 'bg-surface-primary text-text-tertiary'
            return None

    class Dialog(Node):
        def locator(self, selector):
            if '[data-arena-buttons]' in selector:
                return ActiveFilter(self.page, 'filter')
            return super().locator(selector)

    class Page(Picker):
        def locator(self, selector):
            return Dialog(self, 'dialog') if '[role="dialog"]' in selector else Node(self, 'trigger')

    page = Page()
    asyncio.run(_select_model_in_picker(page, 'model-123', category='text'))
    assert ('click', 'filter') not in page.events
    assert ('click', 'option') in page.events
