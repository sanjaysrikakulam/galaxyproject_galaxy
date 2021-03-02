import axios from "axios";
import { rethrowSimple } from "utils/simple-error";
import { getAppRoot } from "onload/loadConfig";

export class Services {
    constructor(options = {}) {
        this.root = options.root || getAppRoot();
    }

    async getLibraries(include_deleted = false) {
        const url = `http://localhost:8080/api/libraries?deleted=${include_deleted}`;
        try {
            const response = await axios.get(url);
            return response.data;
        } catch (e) {
            rethrowSimple(e);
        }
    }
    async saveChanges(lib, onSucess, onError) {
        const url = `http://localhost:8080/api/libraries/${lib.id}`;
        try {
            const response = axios
                .patch(url, lib)
                .then((response) => {
                    onSucess(response.data);
                })
                .catch((error) => {
                    onError(error);
                });
            return response.data;
        } catch (e) {
            rethrowSimple(e);
        }
    }
    async deleteLibrary(lib, onSucess, onError, isUndelete = false) {
        const url = `http://localhost:8080/api/libraries/${lib.id}${isUndelete ? "?undelete=true" : ""}`;
        try {
            const response = axios
                .delete(url, lib)
                .then((response) => {
                    onSucess(response.data);
                })
                .catch((error) => {
                    onError(error);
                });
            return response.data;
        } catch (e) {
            rethrowSimple(e);
        }
    }
    async createNewLibrary(name, description, synopsis, onSucess, onError) {
        const url = `http://localhost:8080/api/libraries/`;
        try {
            const response = axios
                .post(url, {
                    name: name,
                    description: description,
                    synopsis: synopsis,
                })
                .then((response) => {
                    onSucess(response.data);
                })
                .catch((error) => {
                    onError(error);
                });
            return response.data;
        } catch (e) {
            rethrowSimple(e);
        }
    }
}
