// dangerfile.ts
// Copyright (C) 2020 Presidenza del Consiglio dei Ministri.
// Please refer to the AUTHORS file for more information.
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as
// published by the Free Software Foundation, either version 3 of the
// License, or (at your option) any later version.
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
// GNU Affero General Public License for more details.
// You should have received a copy of the GNU Affero General Public License
// along with this program. If not, see <https://www.gnu.org/licenses/>.

import { message } from "./ci/danger/danger";
import checkFormat from "./ci/danger/black";
import checkLinting from "./ci/danger/mypy";
import commitLint from "./ci/danger/commitlint";

export default async () => {
  message(
    "Thank you for submitting a pull request! The team will review your submission as soon as possible."
  );

  await checkFormat();
  await checkLinting();
  await commitLint({ enabled: true, allowedScopes: [] });
};
