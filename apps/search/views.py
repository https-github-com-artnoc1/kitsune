from datetime import datetime, timedelta
from itertools import chain
import json
import re
import time

from django.conf import settings
from django.contrib.sites.models import Site
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils.http import urlquote
from django.views.decorators.cache import cache_page

import jingo
import jinja2
from mobility.decorators import mobile_template
from statsd import statsd
from tower import ugettext as _, ugettext_lazy as _lazy

from search.models import get_search_models
from search.utils import locale_or_default, clean_excerpt, ComposedList
from forums.models import Thread
from questions.models import Question
import search as constants
from search.forms import SearchForm
from search.es_utils import (ESTimeoutError, ESMaxRetryError, ESException,
                             Sphilastic, F)
from sumo.utils import paginate, smart_int
from wiki.models import Document
import waffle


EXCERPT_JOINER = _lazy(u'...', 'between search excerpts')


def jsonp_is_valid(func):
    func_regex = re.compile(r'^[a-zA-Z_\$][a-zA-Z0-9_\$]*'
        + r'(\[[a-zA-Z0-9_\$]*\])*(\.[a-zA-Z0-9_\$]+(\[[a-zA-Z0-9_\$]*\])*)*$')
    return func_regex.match(func)


class ObjectDict(object):
    def __init__(self, source_dict):
        self.__dict__.update(source_dict)


def search_with_es_unified(request, template=None):
    """ES-specific search view"""

    # TODO: Remove this once elastic search bucketed code is gone.
    start = time.time()

    # JSON-specific variables
    is_json = (request.GET.get('format') == 'json')
    callback = request.GET.get('callback', '').strip()
    mimetype = 'application/x-javascript' if callback else 'application/json'

    # Search "Expires" header format
    expires_fmt = '%A, %d %B %Y %H:%M:%S GMT'

    # Check callback is valid
    if is_json and callback and not jsonp_is_valid(callback):
        return HttpResponse(
            json.dumps({'error': _('Invalid callback function.')}),
            mimetype=mimetype, status=400)

    language = locale_or_default(request.GET.get('language', request.locale))
    r = request.GET.copy()
    a = request.GET.get('a', '0')

    # Search default values
    try:
        category = (map(int, r.getlist('category')) or
                    settings.SEARCH_DEFAULT_CATEGORIES)
    except ValueError:
        category = settings.SEARCH_DEFAULT_CATEGORIES
    r.setlist('category', category)

    # Basic form
    if a == '0':
        r['w'] = r.get('w', constants.WHERE_BASIC)
    # Advanced form
    if a == '2':
        r['language'] = language
        r['a'] = '1'

    # TODO: Rewrite so SearchForm is unbound initially and we can use
    # `initial` on the form fields.
    if 'include_archived' not in r:
        r['include_archived'] = False

    search_form = SearchForm(r)

    if not search_form.is_valid() or a == '2':
        if is_json:
            return HttpResponse(
                json.dumps({'error': _('Invalid search data.')}),
                mimetype=mimetype,
                status=400)

        t = template if request.MOBILE else 'search/form.html'
        search_ = jingo.render(request, t,
                               {'advanced': a, 'request': request,
                                'search_form': search_form})
        search_['Cache-Control'] = 'max-age=%s' % \
                                   (settings.SEARCH_CACHE_PERIOD * 60)
        search_['Expires'] = (datetime.utcnow() +
                              timedelta(
                                minutes=settings.SEARCH_CACHE_PERIOD)) \
                              .strftime(expires_fmt)
        return search_

    cleaned = search_form.cleaned_data

    page = max(smart_int(request.GET.get('page')), 1)
    offset = (page - 1) * settings.SEARCH_RESULTS_PER_PAGE

    lang = language.lower()
    if settings.LANGUAGES.get(lang):
        lang_name = settings.LANGUAGES[lang]
    else:
        lang_name = ''

    # Woah! object?! Yeah, so what happens is that Sphilastic is
    # really an elasticutils.S and that requires a Django ORM model
    # argument. That argument only gets used if you want object
    # results--for every hit it gets back from ES, it creates an
    # object of the type of the Django ORM model you passed in. We use
    # object here to satisfy the need for a type in the constructor
    # and make sure we don't ever ask for object results.
    searcher = Sphilastic(object)

    wiki_f = F()
    question_f = F()
    discussion_f = F()

    # Start - wiki filters

    if cleaned['w'] & constants.WHERE_WIKI:
        # Category filter
        if cleaned['category']:
            wiki_f &= F(document_category__in=cleaned['category'])

        # Locale filter
        wiki_f &= F(document_locale=language)

        # Product filter
        products = cleaned['product']
        for p in products:
            wiki_f &= F(document_tag=p)

        # Tags filter
        tags = [t.strip() for t in cleaned['tags'].split()]
        for t in tags:
            wiki_f &= F(document_tag=t)

        # Archived bit
        if a == '0' and not cleaned['include_archived']:
            # Default to NO for basic search:
            cleaned['include_archived'] = False
        if not cleaned['include_archived']:
            wiki_f &= F(document_is_archived=False)

    # End - wiki filters

    # Start - support questions filters

    if cleaned['w'] & constants.WHERE_SUPPORT:

        # Solved is set by default if using basic search
        if a == '0' and not cleaned['has_helpful']:
            cleaned['has_helpful'] = constants.TERNARY_YES

        # These filters are ternary, they can be either YES, NO, or OFF
        ternary_filters = ('is_locked', 'is_solved', 'has_answers',
                           'has_helpful')
        d = dict(('question_%s' % filter_name,
                  _ternary_filter(cleaned[filter_name]))
                 for filter_name in ternary_filters if cleaned[filter_name])
        if d:
            question_f &= F(**d)

        if cleaned['asked_by']:
            question_f &= F(question_creator=cleaned['asked_by'])

        if cleaned['answered_by']:
            question_f &= F(question_answer_creator=cleaned['answered_by'])

        q_tags = [t.strip() for t in cleaned['q_tags'].split()]
        for t in q_tags:
            question_f &= F(question_tag=t)

    # End - support questions filters

    # Start - discussion forum filters

    if cleaned['w'] & constants.WHERE_DISCUSSION:
        if cleaned['author']:
            discussion_f &= F(post_author_ord=cleaned['author'])

        if cleaned['thread_type']:
            if constants.DISCUSSION_STICKY in cleaned['thread_type']:
                discussion_f &= F(post_is_sticky=1)

            if constants.DISCUSSION_LOCKED in cleaned['thread_type']:
                discussion_f &= F(post_is_locked=1)

        if cleaned['forum']:
            discussion_f &= F(post_form_id__in=cleaned['forum'])

    # End - discussion forum filters

    # Created filter
    unix_now = int(time.time())
    interval_filters = (
        ('created', cleaned['created'], cleaned['created_date']),
        ('updated', cleaned['updated'], cleaned['updated_date']))
    for filter_name, filter_option, filter_date in interval_filters:
        if filter_option == constants.INTERVAL_BEFORE:
            before = {filter_name + '__gte': 0,
                      filter_name + '__lte': max(filter_date, 0)}

            discussion_f &= F(**before)
            question_f &= F(**before)
        elif filter_option == constants.INTERVAL_AFTER:
            after = {filter_name + '__gte': min(filter_date, unix_now),
                     filter_name + '__lte': unix_now}

            discussion_f &= F(**after)
            question_f &= F(**after)

    # Note: num_voted (with a d) is a different field than num_votes
    # (with an s). The former is a dropdown and the latter is an
    # integer value.
    if cleaned['num_voted'] == constants.INTERVAL_BEFORE:
        question_f &= F(question_num_votes__lte=max(cleaned['num_votes'], 0))
    elif cleaned['num_voted'] == constants.INTERVAL_AFTER:
        question_f &= F(question_num_votes__gte=cleaned['num_votes'])

    # Done with all the filtery stuff--time  to generate results

    documents = ComposedList()
    try:
        cleaned_q = cleaned['q']

        # Add all the filters
        searcher = searcher.filter(question_f | wiki_f | discussion_f)

        # Set up the highlights
        searcher = searcher.highlight(
            'question_title', 'question_content', 'question_answer_content',
            'discussion_content',
            before_match='<b>',
            after_match='</b>',
            limit=settings.SEARCH_SUMMARY_LENGTH)

        # Set up weights
        searcher = searcher.weight(
            question_title__text=4, question_content__text=3,
            question_answer_content__text=3,
            post_title__text=2, post_content__text=1,
            document_title__text=6, document_content__text=1,
            document_keywords__text=4, document_summary__text=2)

        # Apply sortby, but only for advanced search for questions
        if a == '1' and cleaned['w'] & constants.WHERE_SUPPORT:
            sortby = smart_int(request.GET.get('sortby'))
            try:
                searcher = searcher.order_by(
                    *constants.SORT_QUESTIONS_ES[sortby])
            except IndexError:
                # Skip index errors because they imply the user is
                # sending us sortby values that aren't valid.
                pass

        # Build the query
        if cleaned_q:
            query_fields = chain(*[cls.get_query_fields()
                                   for cls in get_search_models()])

            query = dict((field, cleaned_q) for field in query_fields)

            searcher = searcher.query(or_=query)

        # TODO - Can ditch the ComposedList here, but we need
        # something that paginate can use to figure out the paging.
        documents = ComposedList()
        documents.set_count(('results', searcher),
                            min(searcher.count(), settings.SEARCH_MAX_RESULTS))

        results_per_page = settings.SEARCH_RESULTS_PER_PAGE
        pages = paginate(request, documents, results_per_page)
        num_results = len(documents)

        # Get the documents we want to show and add them to
        # docs_for_page
        documents = documents[offset:offset + results_per_page]

        bounds = documents[0][1]
        searcher = searcher.values_dict()[bounds[0]:bounds[1]]

        results = []
        for i, doc in enumerate(searcher):
            rank = i + offset

            if doc['model'] == 'wiki_document':
                summary = doc['document_summary']
                result = {
                    'title': doc['document_title'],
                    'type': 'document'}

            elif doc['model'] == 'questions_question':
                summary = _build_es_excerpt(doc)
                result = {
                    'title': doc['question_title'],
                    'type': 'question',
                    'is_solved': doc['question_is_solved'],
                    'num_answers': doc['question_num_answers'],
                    'num_votes': doc['question_num_votes'],
                    'num_votes_past_week': doc['question_num_votes_past_week']}

            else:
                summary = _build_es_excerpt(doc)
                result = {
                    'title': doc['post_title'],
                    'type': 'thread'}

            result['url'] = doc['url']
            result['object'] = ObjectDict(doc)
            result['search_summary'] = summary
            result['rank'] = rank
            result['score'] = doc._score
            results.append(result)

    except (ESTimeoutError, ESMaxRetryError, ESException), exc:
        # Handle timeout and all those other transient errors with a
        # "Search Unavailable" rather than a Django error page.
        if is_json:
            return HttpResponse(json.dumps({'error':
                                             _('Search Unavailable')}),
                                mimetype=mimetype, status=503)

        if isinstance(exc, ESTimeoutError):
            statsd.incr('search.esunified.timeouterror')
        elif isinstance(exc, ESMaxRetryError):
            statsd.incr('search.esunified.maxretryerror')
        elif isinstance(exc, ESException):
            statsd.incr('search.esunified.elasticsearchexception')

        import logging
        logging.exception(exc)

        t = 'search/mobile/down.html' if request.MOBILE else 'search/down.html'
        return jingo.render(request, t, {'q': cleaned['q']}, status=503)

    items = [(k, v) for k in search_form.fields for
             v in r.getlist(k) if v and k != 'a']
    items.append(('a', '2'))

    if is_json:
        # Models are not json serializable.
        for r in results:
            del r['object']
        data = {}
        data['results'] = results
        data['total'] = len(results)
        data['query'] = cleaned['q']
        if not results:
            data['message'] = _('No pages matched the search criteria')
        json_data = json.dumps(data)
        if callback:
            json_data = callback + '(' + json_data + ');'

        return HttpResponse(json_data, mimetype=mimetype)

    results_ = jingo.render(request, template,
        {'num_results': num_results, 'results': results, 'q': cleaned['q'],
         'pages': pages, 'w': cleaned['w'],
         'search_form': search_form, 'lang_name': lang_name, })
    results_['Cache-Control'] = 'max-age=%s' % \
                                (settings.SEARCH_CACHE_PERIOD * 60)
    results_['Expires'] = (datetime.utcnow() +
                           timedelta(minutes=settings.SEARCH_CACHE_PERIOD)) \
                           .strftime(expires_fmt)
    results_.set_cookie(settings.LAST_SEARCH_COOKIE, urlquote(cleaned['q']),
                        max_age=3600, secure=False, httponly=False)

    # TODO: Remove this once elastic search bucketed is gone
    dt = (time.time() - start) * 1000
    statsd.timing('search.elastic.unified.view', int(dt))

    return results_


@mobile_template('search/{mobile/}results.html')
def search(request, template=None):
    """ES-specific search view"""

    if (waffle.flag_is_active(request, 'esunified') or
        request.GET.get('esunified')):
        return search_with_es_unified(request, template)

    start = time.time()

    # JSON-specific variables
    is_json = (request.GET.get('format') == 'json')
    callback = request.GET.get('callback', '').strip()
    mimetype = 'application/x-javascript' if callback else 'application/json'

    # Search "Expires" header format
    expires_fmt = '%A, %d %B %Y %H:%M:%S GMT'

    # Check callback is valid
    if is_json and callback and not jsonp_is_valid(callback):
        return HttpResponse(
            json.dumps({'error': _('Invalid callback function.')}),
            mimetype=mimetype, status=400)

    language = locale_or_default(request.GET.get('language', request.locale))
    r = request.GET.copy()
    a = request.GET.get('a', '0')

    # Search default values
    try:
        category = (map(int, r.getlist('category')) or
                    settings.SEARCH_DEFAULT_CATEGORIES)
    except ValueError:
        category = settings.SEARCH_DEFAULT_CATEGORIES
    r.setlist('category', category)

    # Basic form
    if a == '0':
        r['w'] = r.get('w', constants.WHERE_BASIC)
    # Advanced form
    if a == '2':
        r['language'] = language
        r['a'] = '1'

    # TODO: Rewrite so SearchForm is unbound initially and we can use
    # `initial` on the form fields.
    if 'include_archived' not in r:
        r['include_archived'] = False

    search_form = SearchForm(r)

    if not search_form.is_valid() or a == '2':
        if is_json:
            return HttpResponse(
                json.dumps({'error': _('Invalid search data.')}),
                mimetype=mimetype,
                status=400)

        t = template if request.MOBILE else 'search/form.html'
        search_ = jingo.render(request, t,
                               {'advanced': a, 'request': request,
                                'search_form': search_form})
        search_['Cache-Control'] = 'max-age=%s' % \
                                   (settings.SEARCH_CACHE_PERIOD * 60)
        search_['Expires'] = (datetime.utcnow() +
                              timedelta(
                                minutes=settings.SEARCH_CACHE_PERIOD)) \
                              .strftime(expires_fmt)
        return search_

    cleaned = search_form.cleaned_data

    page = max(smart_int(request.GET.get('page')), 1)
    offset = (page - 1) * settings.SEARCH_RESULTS_PER_PAGE

    lang = language.lower()
    if settings.LANGUAGES.get(lang):
        lang_name = settings.LANGUAGES[lang]
    else:
        lang_name = ''

    wiki_s = Document.search()
    question_s = Question.search()
    discussion_s = Thread.search()

    # wiki filters
    # Category filter
    if cleaned['category']:
        wiki_s = wiki_s.filter(document_category__in=cleaned['category'])

    # Locale filter
    wiki_s = wiki_s.filter(document_locale=language)

    # Product filter
    products = cleaned['product']
    for p in products:
        wiki_s = wiki_s.filter(document_tag=p)

    # Tags filter
    tags = [t.strip() for t in cleaned['tags'].split()]
    for t in tags:
        wiki_s = wiki_s.filter(document_tag=t)

    # Archived bit
    if a == '0' and not cleaned['include_archived']:
        # Default to NO for basic search:
        cleaned['include_archived'] = False
    if not cleaned['include_archived']:
        wiki_s = wiki_s.filter(document_is_archived=False)
    # End of wiki filters

    # Support questions specific filters
    if cleaned['w'] & constants.WHERE_SUPPORT:

        # Solved is set by default if using basic search
        if a == '0' and not cleaned['has_helpful']:
            cleaned['has_helpful'] = constants.TERNARY_YES

        # These filters are ternary, they can be either YES, NO, or OFF
        ternary_filters = ('is_locked', 'is_solved', 'has_answers',
                           'has_helpful')
        d = dict(('question_%s' % filter_name,
                  _ternary_filter(cleaned[filter_name]))
                 for filter_name in ternary_filters if cleaned[filter_name])
        if d:
            question_s = question_s.filter(**d)

        if cleaned['asked_by']:
            question_s = question_s.filter(
                question_creator=cleaned['asked_by'])

        if cleaned['answered_by']:
            question_s = question_s.filter(
                question_answer_creator=cleaned['answered_by'])

        q_tags = [t.strip() for t in cleaned['q_tags'].split()]
        for t in q_tags:
            question_s = question_s.filter(question_tag=t)

    # Discussion forum specific filters
    if cleaned['w'] & constants.WHERE_DISCUSSION:
        if cleaned['author']:
            discussion_s = discussion_s.filter(
                post_author_ord=cleaned['author'])

        if cleaned['thread_type']:
            if constants.DISCUSSION_STICKY in cleaned['thread_type']:
                discussion_s = discussion_s.filter(post_is_sticky=1)

            if constants.DISCUSSION_LOCKED in cleaned['thread_type']:
                discussion_s = discussion_s.filter(post_is_locked=1)

        if cleaned['forum']:
            discussion_s = discussion_s.filter(
                post_forum_id__in=cleaned['forum'])

    # Filters common to support and discussion forums
    # Created filter
    unix_now = int(time.time())
    interval_filters = (
        ('created', cleaned['created'], cleaned['created_date']),
        ('updated', cleaned['updated'], cleaned['updated_date']))
    for filter_name, filter_option, filter_date in interval_filters:
        if filter_option == constants.INTERVAL_BEFORE:
            before = {filter_name + '__gte': 0,
                      filter_name + '__lte': max(filter_date, 0)}

            discussion_s = discussion_s.filter(**before)
            question_s = question_s.filter(**before)
        elif filter_option == constants.INTERVAL_AFTER:
            after = {filter_name + '__gte': min(filter_date, unix_now),
                     filter_name + '__lte': unix_now}

            discussion_s = discussion_s.filter(**after)
            question_s = question_s.filter(**after)

    # Note: num_voted (with a d) is a different field than num_votes
    # (with an s). The former is a dropdown and the latter is an
    # integer value.
    if cleaned['num_voted'] == constants.INTERVAL_BEFORE:
        question_s = question_s.filter(
            question_num_votes__lte=max(cleaned['num_votes'], 0))
    elif cleaned['num_voted'] == constants.INTERVAL_AFTER:
        question_s = question_s.filter(
            question_num_votes__gte=cleaned['num_votes'])

    # Done with all the filtery stuff--time  to generate results

    documents = ComposedList()
    sortby = smart_int(request.GET.get('sortby'))
    try:
        max_results = settings.SEARCH_MAX_RESULTS
        cleaned_q = cleaned['q']

        if cleaned['w'] & constants.WHERE_WIKI:
            if cleaned_q:
                wiki_s = wiki_s.query(cleaned_q)

            # For a front-page non-advanced search, we want to cap the kb
            # at 10 results.
            if a == '0':
                wiki_max_results = 10
            else:
                wiki_max_results = max_results
            documents.set_count(('wiki', wiki_s),
                                min(wiki_s.count(), wiki_max_results))

        if cleaned['w'] & constants.WHERE_SUPPORT:
            # Sort results by
            try:
                question_s = question_s.order_by(
                    *constants.SORT_QUESTIONS_ES[sortby])
            except IndexError:
                pass

            question_s = question_s.highlight(
                'question_title', 'question_content',
                'question_answer_content',
                before_match='<b>',
                after_match='</b>',
                limit=settings.SEARCH_SUMMARY_LENGTH)

            if cleaned_q:
                question_s = question_s.query(cleaned_q)
            documents.set_count(('question', question_s),
                                min(question_s.count(), max_results))

        if cleaned['w'] & constants.WHERE_DISCUSSION:
            discussion_s = discussion_s.highlight(
                'discussion_content',
                before_match='<b>',
                after_match='</b>',
                limit=settings.SEARCH_SUMMARY_LENGTH)

            if cleaned_q:
                discussion_s = discussion_s.query(cleaned_q)
            documents.set_count(('forum', discussion_s),
                                min(discussion_s.count(), max_results))

        results_per_page = settings.SEARCH_RESULTS_PER_PAGE
        pages = paginate(request, documents, results_per_page)
        num_results = len(documents)

        # Get the documents we want to show and add them to
        # docs_for_page.
        documents = documents[offset:offset + results_per_page]
        docs_for_page = []
        for (kind, search_s), bounds in documents:
            search_s = search_s.values_dict()[bounds[0]:bounds[1]]
            docs_for_page += [(kind, doc) for doc in search_s]

        results = []
        for i, docinfo in enumerate(docs_for_page):
            rank = i + offset
            # Type here is something like 'wiki', ... while doc here
            # is an ES result document.
            type_, doc = docinfo

            if type_ == 'wiki':
                summary = doc['document_summary']
                result = {
                    'url': doc['url'],
                    'title': doc['document_title'],
                    'type': 'document',
                    'object': ObjectDict(doc)}
            elif type_ == 'question':
                summary = _build_es_excerpt(doc)
                result = {
                    'url': doc['url'],
                    'title': doc['question_title'],
                    'type': 'question',
                    'object': ObjectDict(doc),
                    'is_solved': doc['question_is_solved'],
                    'num_answers': doc['question_num_answers'],
                    'num_votes': doc['question_num_votes'],
                    'num_votes_past_week': doc['question_num_votes_past_week']}
            else:
                summary = _build_es_excerpt(doc)
                result = {
                    'url': doc['url'],
                    'title': doc['post_title'],
                    'type': 'thread',
                    'object': ObjectDict(doc)}
            result['search_summary'] = summary
            result['rank'] = rank
            result['score'] = doc._score
            results.append(result)

    except (ESTimeoutError, ESMaxRetryError, ESException), exc:
        # Handle timeout and all those other transient errors with a
        # "Search Unavailable" rather than a Django error page.
        if is_json:
            return HttpResponse(json.dumps({'error':
                                             _('Search Unavailable')}),
                                mimetype=mimetype, status=503)

        if isinstance(exc, ESTimeoutError):
            statsd.incr('search.es.timeouterror')
        elif isinstance(exc, ESMaxRetryError):
            statsd.incr('search.es.maxretryerror')
        elif isinstance(exc, ESException):
            statsd.incr('search.es.elasticsearchexception')

        t = 'search/mobile/down.html' if request.MOBILE else 'search/down.html'
        return jingo.render(request, t, {'q': cleaned['q']}, status=503)

    items = [(k, v) for k in search_form.fields for
             v in r.getlist(k) if v and k != 'a']
    items.append(('a', '2'))

    if is_json:
        # Models are not json serializable.
        for r in results:
            del r['object']
        data = {}
        data['results'] = results
        data['total'] = len(results)
        data['query'] = cleaned['q']
        if not results:
            data['message'] = _('No pages matched the search criteria')
        json_data = json.dumps(data)
        if callback:
            json_data = callback + '(' + json_data + ');'

        return HttpResponse(json_data, mimetype=mimetype)

    results_ = jingo.render(request, template,
        {'num_results': num_results, 'results': results, 'q': cleaned['q'],
         'pages': pages, 'w': cleaned['w'],
         'search_form': search_form, 'lang_name': lang_name, })
    results_['Cache-Control'] = 'max-age=%s' % \
                                (settings.SEARCH_CACHE_PERIOD * 60)
    results_['Expires'] = (datetime.utcnow() +
                           timedelta(minutes=settings.SEARCH_CACHE_PERIOD)) \
                           .strftime(expires_fmt)
    results_.set_cookie(settings.LAST_SEARCH_COOKIE, urlquote(cleaned['q']),
                        max_age=3600, secure=False, httponly=False)

    # Send timing information for each engine. Bug 723930.
    # TODO: Remove this once Sphinx is gone.
    dt = (time.time() - start) * 1000
    statsd.timing('search.elastic.bucketed.view', int(dt))

    return results_


@cache_page(60 * 15)  # 15 minutes.
def suggestions(request):
    """A simple search view that returns OpenSearch suggestions."""
    mimetype = 'application/x-suggestions+json'

    term = request.GET.get('q')
    if not term:
        return HttpResponseBadRequest(mimetype=mimetype)

    site = Site.objects.get_current()
    locale = locale_or_default(request.locale)
    try:
        # This uses .search(). We assume that sets the query_fields.
        # Otherwise the query here won't work.
        wiki_s = (Document.search()
                  .filter(document_is_archived=False)
                  .filter(document_locale=locale)
                  .values_dict('document_title', 'url')
                  .query(term)[:5])
        question_s = (Question.search()
                      .filter(question_has_helpful=True)
                      .values_dict('question_title', 'url')
                      .query(term)[:5])

        results = list(chain(question_s, wiki_s))
    except (ESTimeoutError, ESMaxRetryError, ESException):
        # If we have ES problems, we just send back an empty result
        # set.
        results = []

    urlize = lambda r: u'https://%s%s' % (site, r['url'])
    titleize = lambda r: (r['document_title'] if 'document_title' in r
                          else r['question_title'])
    data = [term,
            [titleize(r) for r in results],
            [],
            [urlize(r) for r in results]]
    return HttpResponse(json.dumps(data), mimetype=mimetype)


@cache_page(60 * 60 * 168)  # 1 week.
def plugin(request):
    """Render an OpenSearch Plugin."""
    site = Site.objects.get_current()
    return jingo.render(request, 'search/plugin.html',
                        {'site': site, 'locale': request.locale},
                        mimetype='application/opensearchdescription+xml')


def _ternary_filter(ternary_value):
    """Return a search query given a TERNARY_YES or TERNARY_NO.

    Behavior for TERNARY_OFF is undefined.

    """
    return ternary_value == constants.TERNARY_YES


def _build_es_excerpt(result):
    """Return concatenated search excerpts.

    :arg result: The result object from the queryset results

    """
    excerpt = EXCERPT_JOINER.join(
        [m.strip() for m in
         chain(*result._highlighted.values()) if m])

    return jinja2.Markup(clean_excerpt(excerpt))
