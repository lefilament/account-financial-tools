This module is an extension of Odoo base_vat module which allows to display VAT validation status.
This module is only useful for EU partners if you would use VAT validation using EU VIES service.

Quite often, VIES validation service responds with a timeout or a MS_MAX_CONCURRENT_REQ error.
In this case, there is no way to request new validation to VIES service.

This module adds validation status next to VAT and a button to request validation in case it is not yet validated.
