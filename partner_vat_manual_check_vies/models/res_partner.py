# Copyright 2025 Le Filament (https://le-filament.com)
# Copyright (c) 2004-2015 Odoo S.A.
# License LGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

import requests

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# request URL
URL_VIES_REST = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{ms}/vat/{vat}"


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

    def _any_company_check_vies(self):
        return (
            self.env["res.company"].sudo().search_count([("vat_check_vies", "=", True)])
        )

    def _cron_check_vies(self, limit=20):
        # Nothing to do if no company uses VIES validation
        if not self._any_company_check_vies():
            return False
        # Partners with unknown VIES validation. Not valid neither invalid.
        partners = self.search(
            [("vies_valid", "!=", True), ("vies_invalid", "!=", True)], limit=limit
        )
        for partner in partners.filtered("vies_vat_to_check"):
            # Avoid pre-fetching after each cache invalidation due to committing.
            partner = partner[0]
            partner._compute_vies_valid()

    def _cron_clear_vat(self):
        # Nothing to do if no company uses VIES validation
        if not self._any_company_check_vies():
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
        if not self._any_company_check_vies():
            self.vies_valid = False
            return

        for partner in self:
            vat = partner.vies_vat_to_check
            if not vat:
                partner.vies_valid = partner.vies_invalid = False
                continue
            if partner.parent_id and partner.parent_id.vies_vat_to_check == vat:
                partner.vies_valid = partner.parent_id.vies_valid
                partner.vies_invalid = partner.parent_id.vies_invalid
                continue
            partner.vies_valid, partner.vies_invalid = (
                partner._do_request_vies_validation()
            )

    def _do_request_vies_validation(self):
        """query the VIES REST API and update the partner accordingly"""
        vat = self.vies_vat_to_check
        try:
            url = URL_VIES_REST.format(ms=vat[0:2], vat=vat)
            _logger.info(f"Calling VIES service to check VAT for validation: {url}")
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                result = response.json()
                isValid = result.get("isValid")
                return isValid, not isValid
            elif response.status_code == 429:
                _logger.warning("Reached VIES rate limit.")
            else:
                _logger.warning(
                    f"VIES request failed with status {response.status_code}."
                )
        except requests.exceptions.RequestException as e:
            _logger.error("Failed to connect to VIES.", e)

        return False, False
