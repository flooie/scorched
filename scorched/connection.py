import itertools
import json
import requests
import scorched.dates
import scorched.response
import scorched.search
import scorched.exc
import time
import urlparse
import urllib
import warnings

MAX_LENGTH_GET_URL = 2048
# Jetty default is 4096; Tomcat default is 8192; picking 2048 to be
# conservative.


class SolrConnection(object):
    readable = True
    writeable = True

    def __init__(self, url, http_connection, mode, retry_timeout,
                 max_length_get_url):
        """
        :param url: url to solr
        :type url: str
        :param http_connection: already existing connection TODO
        :type http_connection: requests connection
        :param mode: mode (readable, writable) solr
        :type mode: str
        :param retry_timeout: timeout until retry
        :type retry_timeout: int
        :param max_length_get_url: max length until switch to post
        :type max_length_get_url: int
        """
        self.http_connection = requests.Session()
        if mode == 'r':
            self.writeable = False
        elif mode == 'w':
            self.readable = False
        self.url = url.rstrip("/") + "/"
        self.update_url = self.url + "update/json"
        self.select_url = self.url + "select/"
        self.mlt_url = self.url + "mlt/"
        self.retry_timeout = retry_timeout
        self.max_length_get_url = max_length_get_url

    def request(self, *args, **kwargs):
        """
        :param args: arguments
        :type args: tuple
        :param kwargs: key word arguments
        :type kwargs: dict

        .. todo::
            Make this api more explicit!
        """
        try:
            return self.http_connection.request(*args, **kwargs)
        except requests.exceptions.ConnectionError:
            if self.retry_timeout < 0:
                raise
            time.sleep(self.retry_timeout)
            return self.http_connection.request(*args, **kwargs)

    def commit(self, waitSearcher=None, expungeDeletes=None, softCommit=None):
        """
        :param waitSearcher: optional -- block until a new searcher is opened
        and registered as the main query searcher, making the changes visible
        :type waitSearcher: bool
        :param expungeDeletes: optional -- merge segments with deletes away
        :type expungeDeletes: bool
        :param softCommit: optional -- perform a soft commit - this will
        refresh the 'view' of the index in a more performant manner, but
        without "on-disk" guarantees.
        :type softCommit: bool

        A commit operation makes index changes visible to new search requests.
        """

        self.update('{"commit": {}}', commit=True,
                    waitSearcher=waitSearcher, expungeDeletes=expungeDeletes,
                    softCommit=softCommit)

    def optimize(self, waitSearcher=None, maxSegments=None):
        """
        :param waitSearcher: optional -- block until a new searcher is opened
        and registered as the main query searcher, making the changes visible
        :type waitSearcher: bool
        :param maxSegments: optional -- optimizes down to at most this number
        of segments
        :type maxSegments: int

        An optimize is like a hard commit except that it forces all of the
        index segments to be merged into a single segment first.
        """
        self.update('{"optimize": {}}', optimize=True,
                    waitSearcher=waitSearcher, maxSegments=maxSegments)

    def rollback(self):
        """
        The rollback command rollbacks all add/deletes made to the index since
        the last commit
        """
        self.update('{"rollback": {}}')

    def update(self, update_doc, **kwargs):
        if not self.writeable:
            raise TypeError("This Solr instance is only for reading")
        body = update_doc
        if body:
            headers = {"Content-Type": "application/json; charset=utf-8"}
        else:
            headers = {}
        url = self.url_for_update(**kwargs)
        response = self.request('POST', url, data=body, headers=headers)
        if response.status_code != 200:
            raise scorched.exc.SolrError(response)

    def url_for_update(self, commit=None, commitWithin=None, softCommit=None,
                       optimize=None, waitSearcher=None, expungeDeletes=None,
                       maxSegments=None):
        extra_params = {}
        if commit is not None:
            extra_params['commit'] = "true" if commit else "false"
        if commitWithin is not None:
            try:
                extra_params['commitWithin'] = str(int(commitWithin))
            except (TypeError, ValueError):
                raise ValueError(
                    "commitWithin should be a number in milliseconds")
            if extra_params['commitWithin'] < 0:
                raise ValueError(
                    "commitWithin should be a number in milliseconds")
        if softCommit is not None:
            extra_params['softCommit'] = "true" if softCommit else "false"
        if optimize is not None:
            extra_params['optimize'] = "true" if optimize else "false"
        if waitSearcher is not None:
            extra_params['waitSearcher'] = "true" if waitSearcher else "false"
        if expungeDeletes is not None:
            extra_params[
                'expungeDeletes'] = "true" if expungeDeletes else "false"
        if maxSegments is not None:
            try:
                extra_params['maxSegments'] = str(int(maxSegments))
            except (TypeError, ValueError):
                raise ValueError("maxSegments")
            if extra_params['maxSegments'] <= 0:
                raise ValueError("maxSegments should be a positive number")
        if 'expungeDeletes' in extra_params and 'commit' not in extra_params:
            raise ValueError("Can't do expungeDeletes without commit")
        if 'maxSegments' in extra_params and 'optimize' not in extra_params:
            raise ValueError("Can't do maxSegments without optimize")
        if extra_params:
            return "%s?%s" % (self.update_url, urllib.urlencode(
                sorted(extra_params.items())))
        else:
            return self.update_url

    def select(self, params):
        if not self.readable:
            raise TypeError("This Solr instance is only for writing")
        params.append(('wt', 'json'))
        qs = urllib.urlencode(params)
        url = "%s?%s" % (self.select_url, qs)
        if len(url) > self.max_length_get_url:
            warnings.warn(
                "Long query URL encountered - POSTing instead of "
                "GETting. This query will not be cached at the HTTP layer")
            url = self.select_url
            method = 'POST'
            kwargs = {
                'data': qs,
                'headers': {
                    "Content-Type": "application/x-www-form-urlencoded"}}
        else:
            method = 'GET'
            kwargs = {}
        response = self.request(method, url, **kwargs)
        if response.status_code != 200:
            raise scorched.exc.SolrError(response)
        return response.content

    def mlt(self, params, content=None):
        """Perform a MoreLikeThis query using the content specified
        There may be no content if stream.url is specified in the params.
        """
        if not self.readable:
            raise TypeError("This Solr instance is only for writing")
        qs = urllib.urlencode(params)
        base_url = "%s?%s" % (self.mlt_url, qs)
        method = 'GET'
        kwargs = {}
        if content is None:
            url = base_url
        else:
            get_url = "%s&stream.body=%s" % (
                base_url, urllib.quote_plus(content))
            if len(get_url) <= self.max_length_get_url:
                url = get_url
            else:
                url = base_url
                method = 'POST'
                kwargs = {
                    'data': content,
                    'headers': {"Content-Type": "text/plain; charset=utf-8"}}
        response = self.request(method, url, **kwargs)
        if response.status_code != 200:
            raise scorched.exc.SolrError(response)
        return response.content


class SolrInterface(object):
    remote_schema_file = "schema?wt=json"

    def __init__(self, url, http_connection=None, mode='',
                 retry_timeout=-1, max_length_get_url=MAX_LENGTH_GET_URL):
        """
        :param url: url to solr
        :type url: str
        :param http_connection: optional -- already existing connection TODO
        :type http_connection: requests connection
        :param mode: optional -- mode (readable, writable) solr
        :type mode: str
        :param retry_timeout: optional -- timeout until retry
        :type retry_timeout: int
        :param max_length_get_url: optional -- max length until switch to post
        :type max_length_get_url: int
        """

        self.conn = SolrConnection(
            url, http_connection, mode, retry_timeout, max_length_get_url)
        self.schema = self.init_schema()
        # we need tuples for endswith
        self._datefields = tuple(self._extract_datefields(self.schema))

    def init_schema(self):
        response = self.conn.request(
            'GET', urlparse.urljoin(self.conn.url, self.remote_schema_file))
        if response.status_code != 200:
            raise EnvironmentError(
                "Couldn't retrieve schema document - status code %s\n%s" % (
                    response.status_code, response.content)
            )
        return response.json()['schema']

    def _extract_datefields(self, schema):
        ret = [x for x in
               schema['fields'] if x['type'] == 'date']
        ret.extend([x for x in schema['dynamicFields']
                    if x['type'] == 'date'])
        if ret:
            ret = [x['name'] for x in ret]
            ret = [x.replace('*', '') for x in ret]
        return ret

    def _prepare_docs(self, docs):
        for doc in docs:
            for name, value in doc.items():
                # XXX remove all None fields this is needed for adding date
                # fields
                if value is None:
                    doc.pop(name)
                    continue
                if name in self._datefields:
                    doc[name] = unicode(scorched.dates.solr_date(value))
                elif name.endswith(self._datefields):
                    doc[name] = unicode(scorched.dates.solr_date(value))
        return docs

    def add(self, docs, chunk=100, **kwargs):
        """
        :param docs: documents to be added
        :type docs: dict
        :param chunk: optional -- size of chunks in witch the add command
        schould be splitted
        :type chunk: int
        :param kwargs: optinal -- additional arguments
        :type kwargs: dict

        Add a document or a list of document to solr.
        """
        if hasattr(docs, "items") or not hasattr(docs, "__iter__"):
            docs = [docs]
        # to avoid making messages too large, we break the message every
        # chunk docs.
        for doc_chunk in grouper(docs, chunk):
            update_message = json.dumps(self._prepare_docs(doc_chunk))
            self.conn.update(update_message, **kwargs)

    def delete_by_query(self, query, **kwargs):
        """
        :param query: criteria how witch entries should be deleted
        :type query: LuceneQuery

        Delete entries by a given query
        """
        delete_message = json.dumps({"delete": {"query": unicode(query)}})
        self.conn.update(delete_message, **kwargs)

    def delete_by_ids(self, ids, **kwargs):
        """
        :param ids: ids of entries that should be deleted
        :type ids: list

        Delete entries by a given id
        """
        delete_message = json.dumps({"delete": ids})
        self.conn.update(delete_message, **kwargs)

    def commit(self, *args, **kwargs):
        """
        Commit actions
        """
        self.conn.commit(*args, **kwargs)

    def optimize(self, *args, **kwargs):
        self.conn.optimize(*args, **kwargs)

    def rollback(self):
        self.conn.rollback()

    def delete_all(self):
        """
        Delete everything
        """
        self.delete_by_query(self.Q(**{"*": "*"}))

    def search(self, **kwargs):
        """
        Search solr
        """
        params = scorched.search.params_from_dict(**kwargs)
        ret = scorched.response.SolrResponse.from_json(
            self.conn.select(params), self._datefields)
        return ret

    def query(self, *args, **kwargs):
        """
        Build a solr query
        """
        q = scorched.search.SolrSearch(self)
        if len(args) + len(kwargs) > 0:
            return q.query(*args, **kwargs)
        else:
            return q

    def mlt_search(self, content=None, **kwargs):
        """
        Mlt search solr
        """
        params = scorched.search.params_from_dict(**kwargs)
        ret = self.schema.parse_response(
            self.conn.mlt(params, content=content))
        return ret

    def mlt_query(self, fields=None, content=None, content_charset=None,
                  url=None, query_fields=None, **kwargs):
        """Perform a similarity query on MoreLikeThisHandler

        The MoreLikeThisHandler is expected to be registered at the '/mlt'
        endpoint in the solrconfig.xml file of the server.

        fields is the list of field names to compute similarity upon. If not
        provided, we just use the default search field.
        query_fields can be used to adjust boosting values on a subset of those
        fields.

        Other MoreLikeThis specific parameters can be passed as kwargs without
        the 'mlt.' prefix.
        """
        q = scorched.search.MltSolrSearch(
            self, content=content, content_charset=content_charset, url=url)
        return q.mlt(fields=fields, query_fields=query_fields, **kwargs)

    def Q(self, *args, **kwargs):
        q = scorched.search.LuceneQuery()
        q.add(args, kwargs)
        return q


def grouper(iterable, n):
    """
    grouper('ABCDEFG', 3) --> [['ABC'], ['DEF'], ['G']]
    """
    i = iter(iterable)
    g = list(itertools.islice(i, 0, n))
    while g:
        yield g
        g = list(itertools.islice(i, 0, n))
