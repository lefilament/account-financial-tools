# Copyright 2026 Le Filament (https://le-filament.com)
# Copyright (c) 2004-2015 Odoo S.A.
# License LGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import logging

import requests
from stdnum.eu.vat import check_vies
from stdnum.exceptions import InvalidComponent

from odoo import _, api, fields, models
from odoo.tools import zeep

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
        """Query the VIES service and update the partner accordingly.

        The API (REST or SOAP) can be configured with ``vies_vat_api`` setting.
        """
        self.ensure_one()
        if self.env.company.vies_vat_api == "soap":
            return self._do_request_vies_validation_soap()
        return self._do_request_vies_validation_rest()

    def _do_request_vies_validation_rest(self):
        """Query the VIES REST API with ``requests``."""
        vat = self.vies_vat_to_check
        try:
            url = URL_VIES_REST.format(ms=vat[0:2], vat=vat)
            _logger.info("Calling VIES REST service to check VAT %s: %s", vat, url)
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                valid = bool(response.json().get("isValid"))
                self._post_vies_status(valid)
                return valid, not valid
            elif response.status_code == 429:
                _logger.warning("Reached VIES rate limit.")
                self._post_vies_message(
                    _(
                        "The VAT number %s could not be validated: the VIES service "
                        "rate limit has been reached. Please try again later.",
                        vat,
                    )
                )
            else:
                _logger.warning(
                    "VIES request failed with status %s.", response.status_code
                )
                self._post_vies_message(
                    _(
                        "The VAT number %(vat)s could not be validated: the VIES "
                        "service responded with status %(status)s.",
                        vat=vat,
                        status=response.status_code,
                    )
                )
        except requests.exceptions.RequestException:
            _logger.error("Failed to connect to VIES.", exc_info=True)
            self._post_vies_message(
                _(
                    "Connection with the VIES server failed. The VAT number %s "
                    "could not be validated.",
                    vat,
                )
            )
        return False, False

    def _do_request_vies_validation_soap(self):
        """Query the VIES SOAP service through the ``stdnum`` library."""
        vat = self.vies_vat_to_check
        try:
            _logger.info("Calling VIES SOAP service to check VAT %s", vat)
            valid = bool(check_vies(vat, timeout=10)["valid"])
            self._post_vies_status(valid)
            return valid, not valid
        except (OSError, InvalidComponent, zeep.exceptions.Fault) as e:
            if isinstance(e, OSError):
                msg = _(
                    "Connection with the VIES server failed. The VAT number %s "
                    "could not be validated.",
                    vat,
                )
            elif isinstance(e, InvalidComponent):
                msg = _(
                    "The VAT number %s could not be interpreted by the VIES server.",
                    vat,
                )
            else:
                msg = _(
                    "The request for VAT validation was not processed. VIES service "
                    "has responded with the following error: %s",
                    e.message,
                )
            self._post_vies_message(msg)
            _logger.warning("The VAT number %s failed VIES check.", vat)
        return False, False

    def _post_vies_status(self, valid):
        """Post the resulting VIES status on the partner chatter."""
        if valid:
            msg = _(
                "The VAT number %s has been validated by the EU VIES service.",
                self.vies_vat_to_check,
            )
        else:
            msg = _(
                "The VAT number %s has been reported as invalid by the EU VIES "
                "service.",
                self.vies_vat_to_check,
            )
        self._post_vies_message(msg)

    def _post_vies_message(self, body):
        """Post a message on the partner chatter, skipping transient records.

        ``_compute_vies_valid`` may run on an onchange (NewId) record, which has
        no chatter to post on; only real, persisted records get a message.
        """
        if self._origin.id:
            self._origin.message_post(body=body)
