import re
import smtplib
import datetime

from django.conf import settings
from django.contrib import auth
from django.db.models import Count
from django.contrib.auth import logout
from django.core.mail import send_mail
from django.template import RequestContext
from django.core.urlresolvers import reverse
from django.template.defaultfilters import slugify
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, render_to_response, get_object_or_404, redirect

from django.core.paginator import EmptyPage
from django.http import HttpResponseGone, HttpResponseNotFound

from shortimer.jobs import models
from shortimer.miner import autotag
from shortimer.paginator import DiggPaginator

def about(request):
    return render(request, 'about.html')

def login(request):
    if request.user.is_authenticated():
        logout(request)
    next = request.GET.get('next', '')
    return render(request, 'login.html', {'next': next})

def logout(request):
    auth.logout(request)
    return redirect('/')

def jobs(request, subject_slug=None):
    jobs = models.Job.objects.filter(published__isnull=False, deleted__isnull=True)
    jobs = jobs.order_by('-published')

    # filter by subject if we were given one
    if subject_slug:
        subject = get_object_or_404(models.Subject, slug=subject_slug)
        jobs = jobs.filter(subjects__in=[subject])
    else: 
        subject = None

    paginator = DiggPaginator(jobs, 20, body=8)
    try:
        page = paginator.page(request.GET.get("page", 1))
    except EmptyPage:
        return HttpResponseNotFound()

    context = {
        'jobs': page.object_list,
        'page': page,
        'paginator': paginator,
        'subject': subject,
    }
    return render(request, 'jobs.html', context)

def feed(request, page=1):
    jobs = models.Job.objects.filter(published__isnull=False, deleted__isnull=True)
    jobs = jobs.order_by('-published')
    updated = jobs[0].updated

    paginator = DiggPaginator(jobs, 25, body=8)
    page = paginator.page(page)

    return render_to_response('feed.xml', 
                              {"page": page, "updated": updated}, 
                              mimetype="application/atom+xml",
                              context_instance=RequestContext(request))

def job(request, id):
    j = get_object_or_404(models.Job, id=id)
    if j.deleted: 
        return HttpResponseGone("Sorry, this job has been deleted.")
    return render(request, "job.html", {"job": j})

@login_required
def job_edit(request, id=None):
    if id:
        j = get_object_or_404(models.Job, id=id)
    else:
        j = models.Job(creator=request.user)

    can_edit_description = _can_edit_description(request.user, j)

    if request.method == "GET":
        context = {"job": j, 
                   "curate_next": request.path == "/curate/employers/",
                   "can_edit_description": can_edit_description, 
                   "error": request.session.pop("error", None),
                   "job_types": models.JOB_TYPES}
        return render(request, "job_edit.html", context)

    form = request.POST
    if form.get("action") == "view":
        return redirect(reverse('job', args=[j.id]))

    _update_job(j, form, request.user)

    if form.get("action") == "autotag":
        autotag(j)

    if form.get("action") == "delete" and not j.published:
        j.deleted = datetime.datetime.now()
        j.save()

    if form.get("action") == "publish":
        publishable, msg = j.publishable()
        if not publishable:
            request.session['error'] = 'Cannot publish yet: %s' % msg
        else:
            j.publish(request.user)

    if request.path.startswith("/curate/"):
        return redirect(request.path)

    if j and not j.deleted:
        return redirect(reverse('job_edit', args=[j.id]))

    return redirect("/") # job was deleted

def _update_job(j, form, user):
    j.title = form.get("title")
    j.url = form.get("url")
    j.contact_name = form.get("contact_name")
    j.contact_email = form.get("contact_email")
    j.job_type = form.get("job_type")

    # set employer
    if form.get("employer", None):
        employer_name = form.get("employer")
        #employer_slug = 
        e, created = models.Employer.objects.get_or_create(
                name=form.get("employer"),
                freebase_id=form.get("employer_freebase_id"))
        j.employer = e

    # only people flagged as staff can edit the job text
    if _can_edit_description(user, j):
        j.description = form.get('description')

    j.save()

    # update subjects
    j.subjects.clear()
    for k in form.keys():
        m = re.match('^subject_(\d+)$', k)
        if not m: continue

        name = form.get(k)
        if not name: continue

        fb_id = form.get("subject_freebase_id_" + m.group(1))
        slug = slugify(name)

        try:
            s = models.Subject.objects.get(slug=slug)
        except models.Subject.DoesNotExist:
            s = models.Subject.objects.create(name=name, freebase_id=fb_id, slug=slug)
        finally:
            j.subjects.add(s)

    # record the edit
    models.JobEdit.objects.create(job=j, user=user)

def user(request, username):
    user = get_object_or_404(auth.models.User, username=username)
    can_edit = request.user.is_authenticated() and user == request.user
    recent_edits = user.edits.all()[0:15]
    return render(request, "user.html", {"user": user, "can_edit": can_edit,
                                         "recent_edits": recent_edits})

def users(request):
    users = auth.models.User.objects.all().order_by('username')
    paginator = DiggPaginator(users, 25, body=8)
    page = paginator.page(request.GET.get("page", 1))
    return render(request, "users.html", 
            {"paginator": paginator, "page": page})

@login_required
def profile(request):
    user = request.user
    profile = user.profile

    if request.method == "POST":
        user.username = request.POST.get("username", user.username)
        user.first_name = request.POST.get("first_name")
        user.last_name = request.POST.get("last_name")
        user.email = request.POST.get("email")
        user.profile.home_url = request.POST.get("home_url")
        user.save()
        user.profile.save()
        return redirect(reverse('user', args=[user.username]))

    return render(request, "profile.html", {"user": user, "profile": profile})

def matcher(request):
    keywords = models.Keyword.objects.all()
    keywords = keywords.annotate(num_jobs=Count("jobs"))
    keywords = keywords.filter(num_jobs__gt=2, ignore=False, subject__isnull=True)
    keywords = keywords.order_by("-num_jobs")

    paginator = DiggPaginator(keywords, 25, body=8)
    page = paginator.page(request.GET.get("page", 1))
    if request.is_ajax():
        template = "matcher_table.html"
    else:
        template = "matcher.html"
    return render(request, template, {"page": page, "paginator": paginator})

def matcher_table(request):
    keywords = _kw()
    paginator = DiggPaginator(keywords, 25, body=8)
    page = paginator.page(request.GET.get("page", 1))
    return render(request, "matcher_table.html", {"page": page})

def keyword(request, id):
    k = get_object_or_404(models.Keyword, id=id)
    if request.method == 'POST':
        if request.POST.get('ignore'):
            k.ignore = True
        elif request.POST.get('unlink'):
            k.subject = None
        k.save()
    return render(request, "keyword.html", {"keyword": k})

def tags(request):
    # add a new subject
    if request.method == "POST":
        s, created = models.Subject.objects.get_or_create(
            name=request.POST.get('subjectName'))
        s.type=request.POST.get('subjectTypeName')
        s.freebase_id=request.POST.get('subjectId')
        s.freebase_type_id=request.POST.get('subjectTypeId')

        kw = models.Keyword.objects.get(id=request.POST.get('keywordId'))
        s.keywords.add(kw)
        s.save()
        return redirect(reverse('subject', args=[s.slug]))

    subjects = models.Subject.objects.filter(jobs__deleted__isnull=True)
    subjects = subjects.annotate(num_jobs=Count("jobs"))
    subjects = subjects.filter(num_jobs__gt=0)
    subjects = subjects.order_by("-num_jobs")
    paginator = DiggPaginator(subjects, 25, body=8)
    page = paginator.page(request.GET.get("page", 1))
    context = {
        "paginator": paginator,
        "page": page
        }
    return render(request, "tags.html", context)

def employers(request):
    employers = models.Employer.objects.all()
    employers = employers.annotate(num_jobs=Count("jobs"))
    employers = employers.filter(num_jobs__gt=0)
    employers = employers.order_by("-num_jobs")
    paginator = DiggPaginator(employers, 25, body=8)
    page = paginator.page(request.GET.get("page", 1))
    context = {
        "paginator": paginator,
        "page": page,
    }
    return render(request, "employers.html", context)

def employer(request, employer_slug):
    employer = get_object_or_404(models.Employer, slug=employer_slug)
    return render(request, "employer.html", {"employer": employer})

def curate(request):
    need_employer = models.Job.objects.filter(employer__isnull=True, deleted__isnull=True).count()
    need_publish = models.Job.objects.filter(published__isnull=True, deleted__isnull=True).count()
    return render(request, "curate.html", {"need_employer": need_employer,
                                           "need_publish": need_publish})

@login_required
def curate_employers(request):
    if request.method == "POST":
        return job_edit(request, request.POST.get('job_id'))

    # get latest job that lacks an employer
    jobs = models.Job.objects.filter(employer__isnull=True, deleted__isnull=True)
    jobs = jobs.order_by('-post_date')
    if jobs.count() == 0:
        return redirect(reverse('curate'))
    job = jobs[0]
    return job_edit(request, job.id)

@login_required
def curate_drafts(request):
    if request.method == "POST":
        return job_edit(request, request.POST.get('job_id'))

    # get latest un-published job
    jobs = models.Job.objects.filter(published__isnull=True, deleted__isnull=True)
    jobs = jobs.order_by('-post_date')
    if jobs.count() == 0:
        return redirect(reverse('curate'))
    job = jobs[0]
    return job_edit(request, job.id)

def reports(request):
    now = datetime.datetime.now()
    m = now - datetime.timedelta(days=31)
    w = now - datetime.timedelta(days=7)
    y = now - datetime.timedelta(days=365)

    hotjobs_m = models.Job.objects.filter(post_date__gte=m, deleted__isnull=True)
    hotjobs_m = hotjobs_m.order_by('-page_views')[0:10]

    hotjobs_w = models.Job.objects.filter(post_date__gte=w, deleted__isnull=True)
    hotjobs_w = hotjobs_w.order_by('-page_views')[0:10]

    subjects_m = models.Subject.objects.filter(jobs__post_date__gte=m, jobs__deleted__isnull=True)
    subjects_m = subjects_m.annotate(num_jobs=Count("jobs"))
    subjects_m = subjects_m.order_by("-num_jobs")
    subjects_m = subjects_m[0:10]

    subjects_y = models.Subject.objects.filter(jobs__post_date__gte=y, jobs__deleted__isnull=True)
    subjects_y = subjects_y.annotate(num_jobs=Count("jobs"))
    subjects_y = subjects_y.order_by("-num_jobs")
    subjects_y = subjects_y[0:10]

    employers_m = models.Employer.objects.filter(jobs__post_date__gte=m, jobs__deleted__isnull=True)
    employers_m = employers_m.annotate(num_jobs=Count("jobs"))
    employers_m = employers_m.order_by("-num_jobs")
    employers_m = employers_m[0:10]

    employers_y = models.Employer.objects.filter(jobs__post_date__gte=y, jobs__deleted__isnull=True)
    employers_y = employers_y.annotate(num_jobs=Count("jobs"))
    employers_y = employers_y.order_by("-num_jobs")
    employers_y = employers_y[0:10]

    return render(request, 'reports.html', {"subjects_m": subjects_m,
                                            "subjects_y": subjects_y,
                                            "employers_m": employers_m,
                                            "employers_y": employers_y,
                                            "hotjobs_w": hotjobs_w,
                                            "hotjobs_m": hotjobs_m})

def _can_edit_description(user, job):
    # only staff or the creator of a job posting can edit the text of the 
    # job description.
    if user.is_staff:
        return True
    elif job.creator == user:
        return True
    elif job.created == None:
        return True
    else:
        return False
