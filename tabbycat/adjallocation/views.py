import json
import logging

from django.views.generic.base import TemplateView, View
from django.http import HttpResponse, HttpResponseBadRequest

from actionlog.mixins import LogActionMixin
from actionlog.models import ActionLogEntry
from breakqual.models import BreakCategory
from draw.models import Debate
from participants.models import Adjudicator, Region
# from participants.utils import regions_ordered
from tournaments.models import Round
from tournaments.mixins import DrawForDragAndDropMixin, RoundMixin, SaveDragAndDropDebateMixin
from utils.mixins import JsonDataResponsePostView, SuperuserRequiredMixin

from .allocator import allocate_adjudicators
from .hungarian import HungarianAllocator
from .models import DebateAdjudicator
from .utils import get_histories, get_conflicts

from utils.misc import reverse_round

logger = logging.getLogger(__name__)


class AdjudicatorAllocationViewBase(DrawForDragAndDropMixin, SuperuserRequiredMixin):

    def get_unallocated_adjudicators(self):
        round = self.get_round()
        unused_adjs = [a.serialize(round) for a in round.unused_adjudicators()]
        unused_adjs = [self.annotate_region_classes(a) for a in unused_adjs]
        return json.dumps(unused_adjs)


class EditAdjudicatorAllocationView(AdjudicatorAllocationViewBase, TemplateView):

    template_name = 'edit_adjudicators.html'
    auto_url = "adjudicators-auto-allocate"
    save_url = "save-debate-panel"

    def annotate_round_info(self, round_info):
        t = self.get_tournament()
        r = self.get_round()
        round_info['updateImportanceURL'] = reverse_round('save-debate-importance', r)
        round_info['scoreMin'] = t.pref('adj_min_score')
        round_info['scoreMax'] = t.pref('adj_max_score')
        round_info['scoreForVote'] = t.pref('adj_min_voting_score')
        round_info['allowDuplicateAllocations'] = t.pref('duplicate_adjs')
        round_info['regions'] = self.get_regions_info()
        round_info['categories'] = self.get_categories_info()
        return round_info

    def get_regions_info(self):
        # Need to extract and annotate regions for the allcoation actions key
        all_regions = [r.serialize for r in Region.objects.order_by('id')]
        for i, r in enumerate(all_regions):
            r['class'] = i
        return all_regions

    def get_categories_info(self):
        # Need to extract and annotate categories for the allcoation actions key
        all_bcs = [c.serialize for c in BreakCategory.objects.filter(
            tournament=self.get_tournament()).order_by('id')]
        for i, bc in enumerate(all_bcs):
            bc['class'] = i
        return all_bcs

    def get_context_data(self, **kwargs):
        kwargs['vueUnusedAdjudicators'] = self.get_unallocated_adjudicators()
        kwargs['vueAdjudicatorConflicts'] = get_conflicts(
            self.get_tournament(), self.get_round())
        kwargs['vueAdjudicatorHistories'] = get_histories(
            self.get_tournament(), self.get_round())
        return super().get_context_data(**kwargs)


class CreateAutoAllocation(LogActionMixin, AdjudicatorAllocationViewBase, JsonDataResponsePostView):

    action_log_type = ActionLogEntry.ACTION_TYPE_ADJUDICATORS_AUTO

    def post_data(self):
        allocate_adjudicators(self.get_round(), HungarianAllocator)
        return {
            'debates': self.get_draw(),
            'unallocatedAdjudicators': self.get_unallocated_adjudicators()
        }

    def post(self, request, *args, **kwargs):
        round = self.get_round()
        if round.draw_status == Round.STATUS_RELEASED:
            return HttpResponseBadRequest("Draw is already released, unrelease draw to redo auto-allocations.")
        if round.draw_status != Round.STATUS_CONFIRMED:
            return HttpResponseBadRequest("Draw is not confirmed, confirm draw to run auto-allocations.")
        self.log_action()
        return super().post(request, *args, **kwargs)


class SaveDebateInfo(SuperuserRequiredMixin, RoundMixin, LogActionMixin, View):
    pass


class SaveDebateImportance(SaveDebateInfo):
    action_log_type = ActionLogEntry.ACTION_TYPE_DEBATE_IMPORTANCE_EDIT

    def post(self, request, *args, **kwargs):
        debate = Debate.objects.get(pk=request.POST.get('debate_id'))
        debate.importance = request.POST.get('importance')
        debate.save()
        self.log_action()
        return HttpResponse()


class SaveDebatePanel(SaveDragAndDropDebateMixin):
    action_log_type = ActionLogEntry.ACTION_TYPE_ADJUDICATORS_SAVE

    def get_moved_item(self, id):
        return Adjudicator.objects.get(pk=id)

    def modify_debate(self, debate, posted_debate):
        panellists = posted_debate['panel']
        message = "Processing change for %s" % debate.id

        # below are DEBUG
        for da in DebateAdjudicator.objects.filter(debate=debate).order_by('type'):
            message += "\nExisting: %s" % da
        for panellist in panellists:
            message += "\nNew: %s %s" % (panellist['adjudicator']['name'], panellist['position'])

        for da in DebateAdjudicator.objects.filter(debate=debate):
            message += "\n\tChecking %s" % da
            match = next((p for p in panellists if p["adjudicator"]["id"] == da.adjudicator.id), None)
            if match:
                message += "\n\t\tExists in panel already %s" % da
                if match['position'] == da.type:
                    message += "\n\t\t\tPASS — Is in same position %s" % da
                else:
                    da.type = match['position']
                    da.save()
                    message += "\n\t\t\tUPDATE — Changed position to %s" % da
                # Updated or not needed to be touched; remove from consideration for adding
                panellists.remove(match)
            else:
                message += "\n\tDELETE — No longer needed; deleting %s" % da
                da.delete()

        for p in panellists:
            adjudicator = Adjudicator.objects.get(pk=p["adjudicator"]["id"])
            new_allocation = DebateAdjudicator.objects.create(debate=debate,
                adjudicator=adjudicator, type=p["position"])
            new_allocation.save() # Move to new location
            message += "\n\tNEW — Creating new allocation %s" % new_allocation

        message += "\n---"
        print(message)
        return debate
