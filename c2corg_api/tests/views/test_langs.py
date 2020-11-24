from c2corg_api.scripts.es.fill_index import fill_index
from c2corg_api.tests.search import force_search_index
from c2corg_api.tests.views import BaseTestRest


class TestLangs(BaseTestRest):
    @classmethod
    def setUpClass(cls):
        cls.langs = (
            "fr",
            "en",
            "de",
            "ca",
            "es",
            "it",
            "eu",
            "zh"
        )
        return super().setUpClass()

    def post_success(self, prefix, body):
        headers = self.add_authorization_header(username="contributor")
        response = self.app_post_json(prefix, body, headers=headers, status=200)

        body = response.json

        return body

    def put_success(self, prefix, body):
        headers = self.add_authorization_header(username="contributor")
        response = self.app_put_json(
            prefix + "/" + str(body["document"]["document_id"]),
            body,
            headers=headers,
            status=200,
        )

        body = response.json

        return body

        # if skip_validation:
        #     document_id = body.get('document_id')
        #     response = self.app.get(
        #         self._prefix + '/' + str(document_id), status=200)
        #     doc = self.session.query(self._model).get(document_id)
        #     return response.json, doc
        # else:
        #     return self._validate_document(body, headers, validate_with_auth)

    def test_create(self):

        for lang in self.langs:
            body = {
                "article_type": "collab",
                "locales": [{"lang": lang, "title": "Title"}],
            }

            document = self.post_success("/articles", body)

            document = self.app.get("/articles/" + str(document["document_id"])).json
            assert lang == document["locales"][0]["lang"]

    def test_modify(self):
        body = {
            "article_type": "collab",
            "locales": [{"lang": "en", "title": "Title"}],
        }

        document = self.post_success("/articles", body)

        for lang in self.langs:
            document = self.app.get("/articles/" + str(document["document_id"])).json
            locales = {l["lang"]: l for l in document["locales"]}

            body = {
                "document": {
                    "document_id": document["document_id"],
                    "version": document["version"],
                    "article_type": "collab",
                    "locales": [{"lang": lang, "title": "Title"}],
                }
            }

            if lang in locales:
                body["document"]["locales"][0]["version"] = locales[lang]["version"]

            self.put_success("/articles", body)

        document = self.app.get("/articles/" + str(document["document_id"])).json
        locales = {l["lang"]: l for l in document["locales"]}

        for lang in self.langs:
            assert lang in locales

    def test_search(self):
        for lang in self.langs:
            body = {
                "article_type": "collab",
                "locales": [{"lang": lang, "title": "Title"}],
            }

            self.post_success("/articles", body)

        self.session.flush()
        fill_index(self.session)
        # make sure the search index is built
        force_search_index()

        for lang in self.langs:
            response = self.app.get("/search" + '?q=Title&pl=' + lang, status=200)
            body = response.json

            self.assertIn('articles', body)

            articles = body['articles']
            self.assertTrue(articles['total'] > 0)

            locales = articles['documents'][0]['locales']
            self.assertEqual(len(locales), 1)
