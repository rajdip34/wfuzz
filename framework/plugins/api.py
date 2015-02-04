from framework.plugins.pluginobjects import PluginResult
from framework.plugins.pluginobjects import PluginRequest
from framework.core.myexception import FuzzException
from framework.core.facade import Facade
from framework.fuzzer.filter import FuzzResFilter

import os
import urlparse
import urllib2
import json
import abc
import itertools

# Util methods when processing fuzz results 

class FuzzResParse(urlparse.ParseResult):
    @staticmethod
    def parse_url(url):
	scheme, netloc, path, params, query, fragment = urlparse.urlparse(url)
	return FuzzResParse(scheme, netloc, path, params, query, fragment)

    @staticmethod
    def parse_res(fuzzres):
	scheme, netloc, path, params, query, fragment = urlparse.urlparse(fuzzres.url)
	return FuzzResParse(scheme, netloc, path, params, query, fragment)

    @property
    def domain(self):
	'''
	Returns domain from an URL. ie. http://www.localhost.com/kk/index.html?id=3
	will return localhost.com
	'''
	return '.'.join(self.netloc.split(":")[0].split(".")[-2:])

    @property
    def file_fullname(self):
	'''
	Returns script plus extension from an URL. ie. http://www.localhost.com/kk/index.html?id=3
	will return index.html
	'''
	u = self.path.split('/')[-1:][0]

	return u

    @property
    def file_extension(self):
	'''
	Returns script extension from an URL. ie. http://www.localhost.com/kk/index.html?id=3
	will return .html
	'''
	return os.path.splitext(self.file_fullname)[1]

    @property
    def file_name(self):
	'''
	Returns script name from an URL. ie. http://www.localhost.com/kk/index.html?id=3
	will return index
	'''
	return os.path.splitext(self.file_fullname)[0]


# Util methods for accessing search results
class BingIter:
    def __init__(self, dork, offset = 0, limit = 0, key = None):
	if key is None:
	    key = Facade().sett.get('plugins', 'bing_apikey')

	if not key:
	    raise FuzzException(FuzzException.FATAL, "An api Bing key is needed. Please chek wfuzz.ini.")

	self._key = key
	self._dork = dork

	self.max_count = 0
	self.current = 0
	self._index = 0
	self._retrieved = 0
	self._results = []

	# first bing request to get estimated total count (it does not take into consideration offset).
	if limit > 0 and limit < 50:
	    total_results, self._retrieved, self._results = self._do_search(offset, limit)
	else:
	    total_results, self._retrieved, self._results = self._do_search(offset)

	# offset not over the results
	if offset > total_results:
	    self._offset = total_results
	else:
	    self._offset = offset

	self.max_count = total_results - self._offset

	# no more than limit results
	if self.max_count > limit and limit > 0:
	    self.max_count = limit

    def _do_search(self, offset = 0, limit = 50):
	# some code taken from http://www.securitybydefault.com/2014/07/search2auditpy-deja-que-bing-haga-el.html?utm_source=feedburner&utm_medium=feed&utm_campaign=Feed%3A+SecurityByDefault+%28Security+By+Default%29
	# api doc http://go.microsoft.com/fwlink/?LinkID=248077
	user_agent = 'Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 5.1; Trident/4.0; FDM; .NET CLR 2.0.50727; InfoPath.2; .NET CLR 1.1.4322)'
	creds = (':%s' % self._key).encode('base64')[:-1]
	auth = 'Basic %s' % creds

	result = None

	try:
	    urlstr = 'https://api.datamarket.azure.com/Data.ashx/Bing/Search/Composite?Sources=%27web%27&Query=%27'+ self._dork +'%27&$format=json'
	    if limit != 50:
		urlstr += "&$top=%d" % limit
	    if offset != 0:
		urlstr += "&$skip=%d" % offset

	    request = urllib2.Request(urlstr)

	    request.add_header('Authorization', auth)
	    request.add_header('User-Agent', user_agent)
	    requestor = urllib2.build_opener()
	    result = requestor.open(request)
	except Exception, e:
	    raise FuzzException(FuzzException.FATAL, "Error when retrieving Bing API results: %s." % e.msg)

	results = json.loads(result.read())

	# WebTotal is not reliable, it is usually much bigger than the actual results, therefore
	# if your offset increases over the real number of results, you get a dict
	# without values and counters to ''. It gets updated when you are close to that limit though.
	if results['d']['results'][0]["WebTotal"]:
	    res_total = int(results['d']['results'][0]["WebTotal"])
	    res_list = results['d']['results'][0]['Web']

	    return res_total, len(res_list), res_list
	else:
	    return 0, 0, 0

    def __iter__(self):
	return self

    def next(self):
	if self.current >= self.max_count:
	    raise StopIteration
	
	# Result buffer already consumed
	if self._index >= self._retrieved:
	    realcount, self._retrieved, self._results = self._do_search(self.current + self._offset)

	    self._index = 0

	    # update real count
	    if self.max_count > realcount:
		self.max_count = realcount

	elem = self._results[self._index]['Url'].strip()

	self.current += 1
	self._index += 1

	# pycurl does not like unicode
	if isinstance(elem, unicode):
	    return elem.encode('utf-8')
	else:
	    return elem

class BasePlugin():
    def __init__(self):
	self.results_queue = None
	self.base_fuzz_res = None

    def run(self, fuzzresult, control_queue, results_queue):
	try:
	    self.results_queue = results_queue
	    self.base_fuzz_res = fuzzresult
	    self.process(fuzzresult)
	except Exception, e:
	    plres = PluginResult()
	    plres.source = "$$exception$$"
	    plres.issue = "Exception within plugin %s: %s" % (self.name, str(e))
	    results_queue.put(plres)
	finally:
	    control_queue.get()
	    control_queue.task_done()
	    return

    def process(self, fuzzresult):
	'''
	This is were the plugin processing is done. Any wfuzz plugin must implement this method, do its job with the fuzzresult received and:
	- queue_url: if it is a discovery plugin enqueing more HTTP request that at some point will generate more results
	- add_result: Add information about the obtained results after the processing with an accurate description

	A kbase (get_kbase, has_kbase, add_kbase) is shared between all plugins. this can be used to store and retrieve relevant "collaborative" information.
	'''
	raise NotImplemented

    def add_result(self, issue):
	plres = PluginResult()
	plres.source = self.name
	plres.issue = issue

	self.results_queue.put(plres)

    def queue_raw_request(self, raw):
	self.results_queue.put(raw)

    def queue_url(self, url):
	self.results_queue.put(PluginRequest.from_fuzzRes(self.base_fuzz_res, url, self.name))

    def get_kbase(self, key):
	v = self.kbase.get(key)
	if not v:
	    raise FuzzException(FuzzException.FATAL, "Key not in kbase")
	return v

    def has_kbase(self, key):
	return self.kbase.has(key)

    def add_kbase(self, key, value):
	self.kbase.add(key, value)

# Plugins specializations with common methods useful for their own type

class DiscoveryPlugin(BasePlugin):
    def __init__(self):
	self.black_list = Facade().sett.get('plugins', 'file_bl').split(",")

	if self.has_kbase("discovery.bl"):
	    self.black_list = self.get_kbase("discovery.bl")[0].split("-")

    def blacklisted_extension(self, url):
	return FuzzResParse.parse_url(url).file_extension in self.black_list

# Payloads helpers

class PayloadTools:
    @staticmethod
    def filter_results(extra_params, itera):
	ffilter = None

	if extra_params and extra_params.has_key("filter"):
	    filter_params = dict(
		active = True,
		regex_show = None,
		codes_show = None,
		codes = [],
		words = [],
		lines = [],
		chars = [],
		regex = None,
		filter_string = extra_params["filter"]
		)
	    ffilter = FuzzResFilter(filter_params)

	    return itertools.ifilter(lambda x:ffilter.is_visible(x), itera)
	else:
	    #raise FuzzException(FuzzException.FATAL, "Missing filter parameter in payload")
	    return itera

    @staticmethod
    def range_results(extra_params, itera):
	offset = None
	limit = None

	if extra_params:
	    if extra_params.has_key("offset"):
		offset = int(extra_params["offset"])

	    if extra_params.has_key("limit"):
		limit = int(extra_params["limit"])

	if offset is None and limit is None:
	    return itera
	else:
	    return itertools.islice(itera, offset, limit)