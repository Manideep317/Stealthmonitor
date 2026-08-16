import langchain
import requests
import re
from playwright.sync_api import Page, expect

class PageOperator:
    def __init__(self, page:Page):
        self.page = page

    def get_page_content(self):
        return self.page.content()

    def get_page_title(self):
        return self.page.title()

    def get_page_url(self):
        return self.page.url

    def get_page_text(self):
        return self.page.inner_text("body")

    def get_page_links(self):
        links = self.page.query_selector_all("a")
        return [link.get_attribute("href") for link in links if link.get_attribute("href")]

