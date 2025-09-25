# Copyright 2025 Le Filament (https://le-filament.com)
# Copyright (c) 2004-2015 Odoo S.A.
# License LGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

from stdnum.eu.vat import check_vies
from stdnum.exceptions import InvalidComponent

from odoo import _, api, fields, models
from odoo.tools import zeep

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Field representing whether VIES validation should be requested
    request_vies_validation = fields.Boolean(compute="_compute_request_vies_validation")
    vies_invalid = fields.Boolean()

    @api.depends_context("company")
    def _compute_request_vies_validation(self):
        self.request_vies_validation = self.env.company.vat_check_vies

    def action_check_vies(self):
        self._compute_vies_valid()

    def action_clear_vat(self):
        for partner in self.filtered("vies_invalid"):
            partner.vat = "/"
            partner.vies_invalid = False

    def _cron_check_vies(self, limit=20):
        # Nothing to do if no company uses VIES validation
        if (
            not self.env["res.company"]
            .sudo()
            .search_count([("vat_check_vies", "=", True)])
        ):
            return False
        partners = self.search(
            [("vies_valid", "!=", True), ("vies_invalid", "!=", True)], limit=limit
        )
        for partner in partners.filtered("vies_vat_to_check"):
            # Avoid pre-fetching after each cache invalidation due to committing.
            partner = partner[0]
            partner._compute_vies_valid()

    def _cron_clear_vat(self):
        # Nothing to do if no company uses VIES validation
        if (
            not self.env["res.company"]
            .sudo()
            .search_count([("vat_check_vies", "=", True)])
        ):
            return False
        partners = self.search(
            [
                ("vies_valid", "!=", True),
                ("vies_invalid", "=", True),
                ("vat", "not in", [False, "/"]),
            ]
        )
        partners.action_clear_vat()

    @api.depends("vies_vat_to_check")
    def _compute_vies_valid(self):
        """
        This function is rewritten from base_vat module (from Odoo v18 CE module)
        Check the VAT number with VIES, if enabled.
        There are 2 changes with respect to Odoo code :
        - sync vies_invalid with parent_id
        - set vies_invalid to True in case VIES reports an invalid VAT ID
        """
        if (
            not self.env["res.company"]
            .sudo()
            .search_count([("vat_check_vies", "=", True)])
        ):
            self.vies_valid = False
            return

        for partner in self:
            if not partner.vies_vat_to_check:
                partner.vies_valid = False
                continue
            if (
                partner.parent_id
                and partner.parent_id.vies_vat_to_check == partner.vies_vat_to_check
            ):
                partner.vies_valid = partner.parent_id.vies_valid
                # Added sync of vies_invalid with parent
                partner.vies_invalid = partner.parent_id.vies_invalid
                continue
            try:
                _logger.info(
                    "Calling VIES service to check VAT for validation: %s",
                    partner.vies_vat_to_check,
                )
                vies_valid = check_vies(partner.vies_vat_to_check, timeout=10)
                partner.vies_valid = vies_valid["valid"]
                # Added set vies_invalid in case VIES reports invalid VAT id
                if not vies_valid["valid"]:
                    partner.vies_invalid = True
            except (OSError, InvalidComponent, zeep.exceptions.Fault) as e:
                if partner._origin.id:
                    msg = ""
                    if isinstance(e, OSError):
                        msg = _(
                            "Connection with the VIES server failed. The VAT number %s "
                            "could not be validated.",
                            partner.vies_vat_to_check,
                        )
                    elif isinstance(e, InvalidComponent):
                        msg = _(
                            "The VAT number %s could not be interpreted by the VIES "
                            "server.",
                            partner.vies_vat_to_check,
                        )
                    elif isinstance(e, zeep.exceptions.Fault):
                        msg = _(
                            "The request for VAT validation was not processed. VIES "
                            "service has responded with the following error: %s",
                            e.message,
                        )
                    partner._origin.message_post(body=msg)
                _logger.warning(
                    "The VAT number %s failed VIES check.", partner.vies_vat_to_check
                )
                partner.vies_valid = False
