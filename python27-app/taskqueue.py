import datetime
import time
import utils
import webapp2
import wsgiref

from google.appengine.api import taskqueue
from google.appengine.ext import deferred
from google.appengine.ext import db
from google.appengine.ext import webapp

from datastore import SDK_CONSISTENCY_WAIT

try:
  import json
except ImportError:
  import simplejson as json


UPDATED_BY_TXN = "TXN_UPDATE"
UPDATED_BY_TQ = "TQ_UPDATE"

# Default Push Queue.
DEFAULT_PUSH_QUEUE = 'default'

# From the GAE docs:
# A queue name can contain uppercase and lowercase letters, numbers,
# and hyphens. The maximum length for a queue name is 100 characters.

# The name of the queue that is used for push queue operations.
PUSH_QUEUE_NAME = "hawkeyepython-PushQueue-0"
# The name of the queue that is used for pull queue operations.
PULL_QUEUE_NAME = "hawkeyepython-PullQueue-0"


class TaskEntity(db.Model):
  value = db.StringProperty(required=True)


class QueueHandler(webapp2.RequestHandler):
  def get(self):
    results = {'queues': [], 'exists': []}
    self.response.set_status(200)

    queue = 'default'
    results['queues'].append(queue)
    try:
      taskqueue.Queue(queue).\
        add(taskqueue.Task(url='/python/taskqueue/clean_up'))
      results['exists'].append(True)
    except taskqueue.UnknownQueueError:
      self.response.set_status(404)
      results['exists'].append(False)

    queue = DEFAULT_PUSH_QUEUE
    results['queues'].append(queue)
    try:
      taskqueue.Queue(queue).\
        add(taskqueue.Task(url='/python/taskqueue/clean_up'))
      results['exists'].append(True)
    except taskqueue.UnknownQueueError:
      self.response.set_status(404)
      results['exists'].append(False)

    queue = PULL_QUEUE_NAME
    results['queues'].append(queue)
    try:
      taskqueue.Queue(queue).\
        add(taskqueue.Task(payload='this is a fake payload'))
      results['exists'].append(True)
    except taskqueue.UnknownQueueError:
      self.response.set_status(404)
      results['exists'].append(False)

    self.response.out.write(json.dumps(results))

class TaskCounterHandler(webapp2.RequestHandler):
  def get(self):
    key = self.request.get('key')
    stats = self.request.get('stats')
    if key is not None and len(key) > 0:
      counter = utils.TaskCounter.get_by_key_name(key)
      if counter is not None:
        self.response.headers['Content-Type'] = "application/json"
        self.response.out.write(json.dumps({ key : counter.count }))
      else:
        self.response.set_status(404)
    elif stats is not None and stats == 'true':
      statsResult = taskqueue.QueueStatistics.fetch(DEFAULT_PUSH_QUEUE)
      self.response.headers['Content-Type'] = "application/json"
      result = {
        'queue' : statsResult.queue.name,
        'tasks' : statsResult.tasks,
        'oldest_eta' : statsResult.oldest_eta_usec,
        'exec_last_minute' : statsResult.executed_last_minute,
        'in_flight' : statsResult.in_flight,
      }
      self.response.out.write(json.dumps(result))

  def post(self):
    key = self.request.get('key')
    get_method = self.request.get('get')
    defer = self.request.get('defer')
    retry = self.request.get('retry')
    backend = self.request.get('backend')
    eta = self.request.get('eta')

    if backend is not None and backend == 'true':
      taskqueue.add(url='/python/taskqueue/worker',
        params={'key': key}, target='hawkeyepython', queue_name=DEFAULT_PUSH_QUEUE)
    elif defer is not None and defer == 'true':
      deferred.defer(utils.process, key)
    elif get_method is not None and get_method == 'true':
      taskqueue.add(url='/python/taskqueue/worker?key=' + key, method='GET',
        queue_name=DEFAULT_PUSH_QUEUE)
    elif eta is not None and eta != '':
      time_now = datetime.datetime.now()
      eta = time_now + datetime.timedelta(0, long(eta))
      taskqueue.add(url='/python/taskqueue/worker', eta=eta, params={'key': key,
        'eta': 'true'}, queue_name=DEFAULT_PUSH_QUEUE)
    else:
      taskqueue.add(url='/python/taskqueue/worker', params={'key': key,
        'retry': retry}, queue_name=DEFAULT_PUSH_QUEUE)
    self.response.headers['Content-Type'] = "application/json"
    self.response.out.write(json.dumps({ 'status' : True }))

  def delete(self):
    db.delete(utils.TaskCounter.all())

class PullTaskHandler(webapp2.RequestHandler):
  def get(self):
    q = taskqueue.Queue('hawkeyepython-pull-queue')
    tasks = q.lease_tasks(3600, 100)
    result = []
    for task in tasks:
      result.append(task.payload)
    q.delete_tasks(tasks)
    self.response.headers['Content-Type'] = "application/json"
    self.response.out.write(json.dumps({ 'tasks' : result }))

  def post(self):
    key = self.request.get('key')
    q = taskqueue.Queue('hawkeyepython-pull-queue')
    q.add([taskqueue.Task(payload=key, method='PULL')])
    self.response.headers['Content-Type'] = "application/json"
    self.response.out.write(json.dumps({ 'status' : True }))

class TransactionalTaskHandler(webapp2.RequestHandler):
  def post(self):
    def task_txn(key, throw_exception):
      taskqueue.add(url='/python/taskqueue/transworker',
        params={'key': key}, transactional=True, queue_name=DEFAULT_PUSH_QUEUE)
      # Enqueue a task update a key, assert that value
      task_ent = TaskEntity(value=UPDATED_BY_TXN, key_name=key) 
      task_ent.put()
      if throw_exception:
        raise
      # Client should poll to see if the task ran correctly

    key = self.request.get('key')

    raise_exception = False
    if self.request.get('raise_exception'):
      raise_exception = True

    assert key != None

    try:
      db.run_in_transaction(task_txn, key, raise_exception)
    except Exception: 
      self.response.out.write(json.dumps({'value' : "None"}))
      return
    else:
      value = TaskEntity.get_by_key_name(key).value
      self.response.out.write(json.dumps({'value' : value}))
     

  def get(self):
    key = self.request.get('key')
    entity = TaskEntity.get_by_key_name(key)
    value = None

    if entity:
      value = entity.value
 
    self.response.out.write(json.dumps({ 'value' : value}))
    
class TransactionalTaskWorker(webapp2.RequestHandler):
  """ Working the streets for transactions. Just trying to get by. Don't judge. 
  """
  def post(self):
    key = self.request.get('key')
    task_ent = TaskEntity.get_by_key_name(key)
    task_ent.value = UPDATED_BY_TQ
    task_ent.put() 

class TaskCounterWorker(webapp2.RequestHandler):
  def get(self):
    utils.process(self.request.get('key'))

  def post(self):
    retry = self.request.get('retry')
    failures = self.request.headers.get("X-AppEngine-TaskRetryCount")
    eta_test = self.request.get('eta')
    eta = self.request.headers.get("X-AppEngine-TaskETA")
    if retry == 'true' and failures == "0":
      raise Exception
    elif eta_test == 'true':
      utils.processEta(self.request.get('key'), eta)
    else:
      utils.process(self.request.get('key'))

class CleanUpTaskEntities(webapp2.RequestHandler):
  def post(self):
    batch_size = 200
    while True:
      query = TaskEntity.all()
      entity_batch = query.fetch(batch_size)
      if not entity_batch:
        self.response.set_status(200)
        return
      entities_fetched = len(entity_batch)
      db.delete(entity_batch)
      time.sleep(SDK_CONSISTENCY_WAIT)
      if entities_fetched < batch_size:
        break
    self.response.set_status(200)

application = webapp.WSGIApplication([
  ('/python/taskqueue/exists', QueueHandler),
  ('/python/taskqueue/counter', TaskCounterHandler),
  ('/python/taskqueue/worker', TaskCounterWorker),
  ('/python/taskqueue/transworker', TransactionalTaskWorker),
  ('/python/taskqueue/trans', TransactionalTaskHandler),
  ('/python/taskqueue/pull', PullTaskHandler),
  ('/python/taskqueue/clean_up', CleanUpTaskEntities),
], debug=True)

if __name__ == '__main__':
  wsgiref.handlers.CGIHandler().run(application)
